"""
Phase 2B.1 #6 — REAL tronpy cryptography, not the FakeChainAdapter's trivial
string-equality scheme used by the rest of the suite. Runs only if tronpy is
actually importable (it is, under the pinned Python 3.12 runtime — see
backend/.python-version and requirements.txt); skips cleanly otherwise so the
rest of the suite still runs on an environment without it.

Uses a freshly-generated, synthetic, test-only TRON keypair
(`PrivateKey.random()`) — never a key that has ever touched testnet/mainnet or
held any value. Nothing here is committed as a fixed key.
"""

import pytest

tronpy = pytest.importorskip("tronpy", reason="tronpy not installed in this environment")

from tronpy.keys import PrivateKey  # noqa: E402

from backend import auth as auth_module  # noqa: E402
from backend.chain import set_chain_adapter_for_tests  # noqa: E402
from backend.chain.tron_adapter import TronEscrowAdapter, verify_tron_signature  # noqa: E402

from .conftest import auth_headers  # noqa: E402


@pytest.fixture()
def synthetic_keypair():
    """Clave privada SINTETICA, generada al vuelo, jamas usada en testnet ni
    mainnet — nunca tuvo ni podria tener valor real."""
    pk = PrivateKey.random()
    return pk, pk.public_key.to_base58check_address()


@pytest.fixture()
def real_tron_adapter_for_tests():
    """TronEscrowAdapter real (misma clase que produccion) pero construido sin
    pasar por __init__ (que requiere un nodo/contrato configurado) —
    verify_wallet_signature no toca self._client/self._contract, asi que esto
    ejercita la MISMA logica de firma real de produccion sin red."""
    adapter = TronEscrowAdapter.__new__(TronEscrowAdapter)
    set_chain_adapter_for_tests(adapter)
    yield adapter
    from backend.chain import reset_chain_adapter_for_tests

    reset_chain_adapter_for_tests()


# --- vectores de prueba puros, contra verify_tron_signature directamente ---


def test_correct_address_verifies(synthetic_keypair):
    pk, address = synthetic_keypair
    message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    signature = pk.sign_msg(message.encode()).hex()
    assert verify_tron_signature(address, message, signature) is True


def test_wrong_address_fails(synthetic_keypair):
    pk, address = synthetic_keypair
    other_pk = PrivateKey.random()
    other_address = other_pk.public_key.to_base58check_address()
    message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    signature = pk.sign_msg(message.encode()).hex()
    assert verify_tron_signature(other_address, message, signature) is False


def test_modified_message_fails(synthetic_keypair):
    pk, address = synthetic_keypair
    message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    signature = pk.sign_msg(message.encode()).hex()
    tampered_message = message + " extra"
    assert verify_tron_signature(address, tampered_message, signature) is False


def test_modified_nonce_fails(synthetic_keypair):
    pk, address = synthetic_keypair
    real_message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    signature = pk.sign_msg(real_message.encode()).hex()
    fabricated_message = auth_module.build_sign_message("0000000000000000000000000000000")
    assert verify_tron_signature(address, fabricated_message, signature) is False


def test_malformed_signature_fails(synthetic_keypair):
    _, address = synthetic_keypair
    message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    assert verify_tron_signature(address, message, "not-hex-at-all") is False
    assert verify_tron_signature(address, message, "deadbeef") is False  # hex valido pero muy corto para una firma
    assert verify_tron_signature(address, message, "") is False


def test_signature_with_0x_prefix_also_verifies(synthetic_keypair):
    pk, address = synthetic_keypair
    message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    signature = "0x" + pk.sign_msg(message.encode()).hex()
    assert verify_tron_signature(address, message, signature) is True


# --- integracion HTTP completa, con el adaptador TRON real inyectado ---


def test_replay_fails_at_auth_layer_with_real_crypto(client, db_session, real_tron_adapter_for_tests, synthetic_keypair):
    pk, address = synthetic_keypair

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address})
    assert nonce_resp.status_code == 200
    message = nonce_resp.json()["message"]
    signature = pk.sign_msg(message.encode()).hex()

    first = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    assert first.status_code == 200
    assert "access_token" in first.json()

    replay = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    assert replay.status_code in (400, 401)
    assert "access_token" not in replay.json()


def test_full_auth_flow_with_real_crypto_succeeds(client, db_session, real_tron_adapter_for_tests, synthetic_keypair):
    pk, address = synthetic_keypair

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address})
    message = nonce_resp.json()["message"]
    signature = pk.sign_msg(message.encode()).hex()

    r = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    assert r.status_code == 200
    token = r.json()["access_token"]

    me = client.get("/me", headers=auth_headers(token))
    assert me.status_code == 200
    assert me.json()["wallet_address"] == address


def test_wrong_keypair_denied_at_auth_layer_with_real_crypto(client, db_session, real_tron_adapter_for_tests, synthetic_keypair):
    pk, address = synthetic_keypair
    attacker_pk = PrivateKey.random()

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address})
    message = nonce_resp.json()["message"]
    wrong_signature = attacker_pk.sign_msg(message.encode()).hex()  # firmado con OTRA clave

    r = client.post("/auth/verify", json={"wallet_address": address, "signature": wrong_signature})
    assert r.status_code == 401
