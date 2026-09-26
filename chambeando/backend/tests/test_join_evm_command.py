"""
Comando explicito "JOIN EVM" (revision 2026-09-25, correccion humana;
renombrado desde "JOIN BSC" por un hallazgo de revision posterior -- ver
backend/chain/auth_networks.py:EVM_AUTH_ONLY_LIMITATIONS para por que "evm",
nunca "bsc" ni ninguna otra cadena EVM especifica, es el nombre honesto de
lo que `personal_sign` realmente prueba):

JOIN heredado solo emitia link_token para settings.CHAIN_ADAPTER ("tron"),
mientras la pagina nueva de vinculacion autentica una wallet EVM -- un
bloqueo funcional real. Este archivo prueba, de punta a punta y con el
router REAL (no un atajo), el comando "JOIN EVM"/"JOINEVM":

1. El comando emite un link_token para "evm", nunca cambia silenciosamente
   la red (el rechazo de red distinta de consume_link_token se preserva
   intacto -- no se toco).
2. El flujo completo (comando -> deep link -> /auth/nonce -> firma EVM real
   -> /auth/verify -> JWT -> /whatsapp/link) funciona para DOS numeros de
   WhatsApp / dos wallets EVM distintas.
3. Controles de rechazo: intercambiar red, intercambiar el link_token entre
   una sesion tron y una evm, e intentar vincular con una segunda wallet.
4. JOIN (heredado, sin sufijo) sigue funcionando exactamente igual --
   confirmado aqui ademas de en test_join_flow_network_consistency.py.
5. Limite de producto: una wallet vinculada por evm jamas recibe un
   deep-link de trading (BUY/SELL) -- el bot lo bloquea explicitamente en
   vez de entregar un link que no podria completarse nunca.
"""

import re

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from backend.chain import reset_chain_adapter_for_tests, set_chain_adapter_for_tests
from backend.chain.tron_adapter import TronEscrowAdapter
from backend.messaging import identity
from backend.messaging.router import ConversationRouter
from backend.messaging.whatsapp_adapter import SandboxMetaClient, WhatsAppAdapter

from .conftest import auth_headers, login

tronpy = pytest.importorskip("tronpy", reason="tronpy not installed in this environment")

from tronpy.keys import PrivateKey  # noqa: E402


@pytest.fixture()
def real_tron_adapter_for_tests():
    adapter = TronEscrowAdapter.__new__(TronEscrowAdapter)
    set_chain_adapter_for_tests(adapter)
    yield adapter
    reset_chain_adapter_for_tests()


def _extract_link(text: str) -> tuple[str, str]:
    match = re.search(r"[?&]token=([^&\s]+)&network=([^&\s]+)", text)
    assert match, f"no se encontro token/network en el mensaje: {text!r}"
    return match.group(1), match.group(2)


def _sign_evm(account, message: str) -> str:
    signed = Account.sign_message(encode_defunct(text=message), private_key=account.key)
    return signed.signature.hex()


def _evm_login(client, account) -> str:
    nonce_resp = client.post("/auth/nonce", json={"wallet_address": account.address, "network_id": "evm"})
    assert nonce_resp.status_code == 200, nonce_resp.text
    message = nonce_resp.json()["message"]
    signature = _sign_evm(account, message)
    verify_resp = client.post(
        "/auth/verify", json={"wallet_address": account.address, "signature": signature, "network_id": "evm"}
    )
    assert verify_resp.status_code == 200, verify_resp.text
    return verify_resp.json()["access_token"]


# ---------------------------------------------------------------------------
# 1) El comando emite el token para la red correcta, nunca la cambia
# ---------------------------------------------------------------------------


def test_join_evm_command_issues_an_evm_link_token(db_session):
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-join-evm-cmd-1", "JOIN EVM")

    to, text = mock.sent[-1]
    token, network = _extract_link(text)
    assert network == "evm"
    assert "TRON" in text  # aclara el limite de producto ANTES de firmar
    assert "NO confirma" in text  # aclara que no se verifica ninguna cadena EVM especifica


def test_joinevm_no_space_alias_also_works(db_session):
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-join-evm-cmd-2", "JOINEVM")

    to, text = mock.sent[-1]
    _, network = _extract_link(text)
    assert network == "evm"


# ---------------------------------------------------------------------------
# 2) Flujo completo, dos numeros / dos wallets EVM reales
# ---------------------------------------------------------------------------


def test_join_evm_full_flow_two_whatsapp_numbers_real_evm_crypto(client, db_session):
    account_a = Account.create()
    account_b = Account.create()

    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)

    router.handle_inbound(db_session, "wa-join-evm-two-a", "JOIN EVM")
    token_a, network_a = _extract_link(mock.sent[-1][1])
    router.handle_inbound(db_session, "wa-join-evm-two-b", "JOIN EVM")
    token_b, network_b = _extract_link(mock.sent[-1][1])
    assert network_a == network_b == "evm"

    jwt_a = _evm_login(client, account_a)
    jwt_b = _evm_login(client, account_b)

    resp_a = client.post("/whatsapp/link", json={"link_token": token_a, "network_id": "evm"}, headers=auth_headers(jwt_a))
    assert resp_a.status_code == 200, resp_a.text
    assert resp_a.json() == {"whatsapp_id": "wa-join-evm-two-a", "linked": True}

    resp_b = client.post("/whatsapp/link", json={"link_token": token_b, "network_id": "evm"}, headers=auth_headers(jwt_b))
    assert resp_b.status_code == 200, resp_b.text
    assert resp_b.json() == {"whatsapp_id": "wa-join-evm-two-b", "linked": True}

    user_a = identity.get_linked_user(db_session, "wa-join-evm-two-a")
    user_b = identity.get_linked_user(db_session, "wa-join-evm-two-b")
    assert user_a.wallet_address == account_a.address
    assert user_b.wallet_address == account_b.address
    assert user_a.network_id == user_b.network_id == "evm"
    assert user_a.id != user_b.id

    # tipear JOIN EVM otra vez, ya vinculado, reporta status en vez de re-emitir un token
    router.handle_inbound(db_session, "wa-join-evm-two-a", "JOIN EVM")
    to, text = mock.sent[-1]
    assert "token=" not in text


# ---------------------------------------------------------------------------
# 3) Controles de rechazo: red intercambiada, enlace intercambiado, wallet intercambiada
# ---------------------------------------------------------------------------


def test_evm_join_link_rejected_when_asserted_under_tron(client, db_session):
    """Red intercambiada: el link_token de un JOIN EVM real no puede
    consumirse afirmando tron -- el rechazo de red distinta de
    consume_link_token se preserva intacto."""
    account = Account.create()
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-join-evm-swap-network-1", "JOIN EVM")
    token, network = _extract_link(mock.sent[-1][1])
    assert network == "evm"

    jwt = _evm_login(client, account)
    resp = client.post("/whatsapp/link", json={"link_token": token, "network_id": "tron"}, headers=auth_headers(jwt))
    assert resp.status_code == 400
    assert "network mismatch" in resp.json()["detail"]
    assert identity.get_linked_user(db_session, "wa-join-evm-swap-network-1") is None


def test_evm_join_link_rejected_with_a_tron_authenticated_jwt(client, db_session, real_tron_adapter_for_tests):
    """Enlace intercambiado entre sesiones de redes distintas: se autentica
    de verdad en TRON (firma TRON real), pero se intenta usar ese JWT
    contra un link_token de JOIN EVM real, afirmando evm en el body -- la
    red REALMENTE autenticada (leida del JWT) no coincide."""
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-join-evm-swap-jwt-1", "JOIN EVM")
    evm_token, network = _extract_link(mock.sent[-1][1])
    assert network == "evm"

    pk = PrivateKey.random()
    address = pk.public_key.to_base58check_address()
    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address, "network_id": "tron"})
    message = nonce_resp.json()["message"]
    signature = pk.sign_msg(message.encode()).hex()
    verify_resp = client.post("/auth/verify", json={"wallet_address": address, "signature": signature, "network_id": "tron"})
    tron_jwt = verify_resp.json()["access_token"]

    resp = client.post("/whatsapp/link", json={"link_token": evm_token, "network_id": "evm"}, headers=auth_headers(tron_jwt))
    assert resp.status_code == 400
    assert "network mismatch" in resp.json()["detail"]
    assert identity.get_linked_user(db_session, "wa-join-evm-swap-jwt-1") is None


def test_tron_join_link_rejected_with_an_evm_authenticated_jwt(client, db_session):
    """El caso simetrico: JOIN (tron, heredado) real, pero se intenta
    consumir con un JWT autenticado de verdad en evm."""
    account = Account.create()
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-join-tron-swap-jwt-1", "JOIN")
    tron_token, network = _extract_link(mock.sent[-1][1])
    assert network == "tron"

    evm_jwt = _evm_login(client, account)
    resp = client.post("/whatsapp/link", json={"link_token": tron_token, "network_id": "tron"}, headers=auth_headers(evm_jwt))
    assert resp.status_code == 400
    assert "network mismatch" in resp.json()["detail"]
    assert identity.get_linked_user(db_session, "wa-join-tron-swap-jwt-1") is None


def test_second_distinct_evm_wallet_rejected_for_already_linked_whatsapp_id(client, db_session):
    """Wallet intercambiada: una vez que un WhatsApp ID ya se vinculo via
    JOIN EVM con la wallet A, una wallet B distinta no puede tomarlo con un
    segundo token, aunque tambien sea evm real."""
    account_a = Account.create()
    account_c = Account.create()

    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-join-evm-already-linked", "JOIN EVM")
    token_1, _ = _extract_link(mock.sent[-1][1])

    jwt_a = _evm_login(client, account_a)
    first = client.post("/whatsapp/link", json={"link_token": token_1, "network_id": "evm"}, headers=auth_headers(jwt_a))
    assert first.status_code == 200, first.text

    token_2 = identity.issue_link_token(db_session, "wa-join-evm-already-linked", 600, network_id="evm")
    jwt_c = _evm_login(client, account_c)
    second = client.post("/whatsapp/link", json={"link_token": token_2, "network_id": "evm"}, headers=auth_headers(jwt_c))
    assert second.status_code == 400
    assert "already linked to a different wallet" in second.json()["detail"]
    assert identity.get_linked_user(db_session, "wa-join-evm-already-linked").wallet_address == account_a.address


# ---------------------------------------------------------------------------
# 4) JOIN heredado (tron) sigue funcionando sin cambios
# ---------------------------------------------------------------------------


def test_legacy_join_tron_still_issues_a_tron_link_token_unchanged(client, db_session, real_tron_adapter_for_tests):
    pk = PrivateKey.random()
    address = pk.public_key.to_base58check_address()

    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-legacy-join-still-works", "JOIN")
    to, text = mock.sent[-1]
    token, network = _extract_link(text)
    assert network == "tron"
    assert text.startswith("Primero necesitamos verificar tu wallet")  # texto original, sin cambios

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address, "network_id": "tron"})
    message = nonce_resp.json()["message"]
    signature = pk.sign_msg(message.encode()).hex()
    verify_resp = client.post("/auth/verify", json={"wallet_address": address, "signature": signature, "network_id": "tron"})
    jwt = verify_resp.json()["access_token"]

    resp = client.post("/whatsapp/link", json={"link_token": token, "network_id": "tron"}, headers=auth_headers(jwt))
    assert resp.status_code == 200, resp.text
    assert identity.get_linked_user(db_session, "wa-legacy-join-still-works").wallet_address == address


# ---------------------------------------------------------------------------
# 5) Limite de producto: sin handoff de trading para una identidad evm
# ---------------------------------------------------------------------------


def test_evm_linked_identity_never_receives_a_trading_handoff_link(client, db_session):
    account = Account.create()
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)

    router.handle_inbound(db_session, "wa-evm-no-trading-1", "JOIN EVM")
    token, _ = _extract_link(mock.sent[-1][1])
    jwt = _evm_login(client, account)
    link_resp = client.post("/whatsapp/link", json={"link_token": token, "network_id": "evm"}, headers=auth_headers(jwt))
    assert link_resp.status_code == 200, link_resp.text

    for command in ("BUY", "SELL", "MY TRADES", "MENU"):
        router.handle_inbound(db_session, "wa-evm-no-trading-1", command)
        to, text = mock.sent[-1]
        assert "http" not in text, f"comando {command!r} genero un handoff con un link -- no deberia con una identidad evm"
        assert "TRON" in text and "evm" in text.lower()


def test_tron_linked_identity_unaffected_still_gets_trading_handoff(client, db_session, real_tron_adapter_for_tests):
    """Control negativo del punto anterior -- una identidad TRON normal
    (la unica red operacional) sigue recibiendo el flujo de BUY/SELL sin
    ningun cambio de comportamiento."""
    from .conftest import seed_membership

    pk = PrivateKey.random()
    address = pk.public_key.to_base58check_address()
    seed_membership(db_session, address)

    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-tron-still-trades-1", "JOIN")
    token, _ = _extract_link(mock.sent[-1][1])

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address, "network_id": "tron"})
    message = nonce_resp.json()["message"]
    signature = pk.sign_msg(message.encode()).hex()
    verify_resp = client.post("/auth/verify", json={"wallet_address": address, "signature": signature, "network_id": "tron"})
    jwt = verify_resp.json()["access_token"]
    client.post("/whatsapp/link", json={"link_token": token, "network_id": "tron"}, headers=auth_headers(jwt))

    router.handle_inbound(db_session, "wa-tron-still-trades-1", "SELL")
    to, text = mock.sent[-1]
    assert "Cuanto cripto queres vender" in text  # el flujo de SELL arranca normalmente, sin bloqueo
