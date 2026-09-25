"""
Auth multichain (revision humana, item 6) -- equivalente EVM de
test_tron_signature_real.py: mismos vectores/flujo HTTP completo, pero con
eth_account real en vez de tronpy real. Usa una clave sintetica generada al
vuelo (Account.create()) -- nunca una clave con valor real.

IMPORTANTE (alcance): esto ejercita auth.py/routers/auth.py en su forma
chain-agnostic ya existente, inyectando un EvmEscrowAdapter real como el
adaptador LEGADO (get_chain_adapter() sin argumento, via
set_chain_adapter_for_tests) -- no valida que la eleccion de red venga del
link/network context (eso lo prueba test_wallet_link_network_binding.py por
separado). El mensaje que se firma aqui demuestra ownership de la wallet;
NO demuestra por si solo que red eligio el usuario -- ver
messaging/identity.py y MULTICHAIN_ARCHITECTURE.md.
"""

import pytest

eth_account = pytest.importorskip("eth_account", reason="eth-account not installed in this environment")

from datetime import timedelta  # noqa: E402

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402

from backend import auth as auth_module  # noqa: E402
from backend import timeutils  # noqa: E402
from backend.chain import reset_chain_adapter_for_tests, set_chain_adapter_for_tests  # noqa: E402
from backend.chain.evm_adapter import EvmEscrowAdapter, verify_evm_signature  # noqa: E402
from backend.models import AuthNonceDB  # noqa: E402

from .conftest import auth_headers  # noqa: E402


@pytest.fixture()
def synthetic_keypair():
    """Clave privada SINTETICA, generada al vuelo, jamas usada en testnet ni
    mainnet -- nunca tuvo ni podria tener valor real."""
    acct = Account.create()
    return acct, acct.address


@pytest.fixture()
def real_evm_adapter_for_tests():
    """EvmEscrowAdapter real (misma clase que produccion) pero construido sin
    pasar por __init__ (que requiere network.rpc_url o un web3_client) --
    verify_wallet_signature/normalize_address no tocan self._w3/self._contract,
    asi que esto ejercita la MISMA logica de firma real de produccion sin red.
    Mismo patron que test_tron_signature_real.py's real_tron_adapter_for_tests."""
    adapter = EvmEscrowAdapter.__new__(EvmEscrowAdapter)
    set_chain_adapter_for_tests(adapter)
    yield adapter
    reset_chain_adapter_for_tests()


def _sign(pk_account, message: str) -> str:
    signed = Account.sign_message(encode_defunct(text=message), private_key=pk_account.key)
    return signed.signature.hex()


# --- vectores de prueba puros, contra verify_evm_signature directamente ---
# (ya cubiertos con mas detalle en test_evm_adapter.py -- repetidos aqui solo
# como vectores minimos, para que este archivo sea autocontenido igual que
# test_tron_signature_real.py)


def test_correct_address_verifies(synthetic_keypair):
    acct, address = synthetic_keypair
    message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    assert verify_evm_signature(address, message, _sign(acct, message)) is True


def test_wrong_address_fails(synthetic_keypair):
    acct, address = synthetic_keypair
    other_acct = Account.create()
    message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    assert verify_evm_signature(other_acct.address, message, _sign(acct, message)) is False


def test_modified_message_fails(synthetic_keypair):
    acct, address = synthetic_keypair
    message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    signature = _sign(acct, message)
    assert verify_evm_signature(address, message + " extra", signature) is False


def test_modified_nonce_fails(synthetic_keypair):
    acct, address = synthetic_keypair
    real_message = auth_module.build_sign_message("deadbeefcafef00d0123456789abcde")
    signature = _sign(acct, real_message)
    fabricated_message = auth_module.build_sign_message("0000000000000000000000000000000")
    assert verify_evm_signature(address, fabricated_message, signature) is False


# --- integracion HTTP completa, con el adaptador EVM real inyectado ---


def test_full_auth_flow_with_real_crypto_succeeds(client, db_session, real_evm_adapter_for_tests, synthetic_keypair):
    acct, address = synthetic_keypair

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address})
    assert nonce_resp.status_code == 200
    message = nonce_resp.json()["message"]
    signature = _sign(acct, message)

    r = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    assert r.status_code == 200
    token = r.json()["access_token"]

    me = client.get("/me", headers=auth_headers(token))
    assert me.status_code == 200
    assert me.json()["wallet_address"] == address


def test_wrong_keypair_denied_at_auth_layer_with_real_crypto(client, db_session, real_evm_adapter_for_tests, synthetic_keypair):
    acct, address = synthetic_keypair
    attacker_acct = Account.create()

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address})
    message = nonce_resp.json()["message"]
    wrong_signature = _sign(attacker_acct, message)  # firmado con OTRA clave

    r = client.post("/auth/verify", json={"wallet_address": address, "signature": wrong_signature})
    assert r.status_code == 401


def test_replay_fails_at_auth_layer_with_real_crypto(client, db_session, real_evm_adapter_for_tests, synthetic_keypair):
    acct, address = synthetic_keypair

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address})
    message = nonce_resp.json()["message"]
    signature = _sign(acct, message)

    first = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    assert first.status_code == 200
    assert "access_token" in first.json()

    replay = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    assert replay.status_code in (400, 401)
    assert "access_token" not in replay.json()


def test_expired_nonce_denied_at_auth_layer_with_real_crypto(client, db_session, real_evm_adapter_for_tests, synthetic_keypair):
    """El vencimiento del nonce es chain-agnostico (auth.verify_and_consume_nonce),
    pero se prueba aqui contra el adaptador EVM real para dejar cubierta la
    combinacion completa (firma EVM real + nonce vencido), no solo TRON."""
    acct, address = synthetic_keypair

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address})
    message = nonce_resp.json()["message"]
    signature = _sign(acct, message)

    # Adelanta el reloj del nonce directamente en la DB -- mismo mecanismo que
    # produccion usaria si el usuario tardara mas de NONCE_EXPIRE_SECONDS en firmar.
    entry = db_session.query(AuthNonceDB).filter(AuthNonceDB.wallet_address == address).order_by(AuthNonceDB.created_at.desc()).first()
    assert entry is not None
    entry.expires_at = timeutils.utcnow() - timedelta(seconds=1)
    db_session.commit()

    r = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    assert r.status_code == 401
    assert "access_token" not in r.json()
