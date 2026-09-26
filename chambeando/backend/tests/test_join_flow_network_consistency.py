"""
Revision de seguridad pedida explicitamente (2026-09-25): "Sigue el flujo
completo JOIN -> /auth/nonce -> /auth/verify -> JWT -> /whatsapp/link.
Comprueba que la red autenticada, la red del token JOIN y la wallet
vinculada coinciden; revisa tambien llamadas internas que omiten
network_id."

Este archivo maneja el router REAL (ConversationRouter.handle_inbound con
"JOIN", no una llamada directa a identity.issue_link_token) para capturar el
deep link exactamente como lo recibiria un usuario real de WhatsApp, y sigue
el flujo de punta a punta con criptografia TRON real.

NOTA HISTORICA (resuelta en una vuelta posterior): cuando este archivo se
escribio, _handle_join (messaging/router.py) era la UNICA via de JOIN --
JOIN EVM (comando explicito, ver test_join_evm_command.py) todavia no
existia, asi que el bot no tenia forma de emitir un link_token para una
red EVM. Eso ya no es asi: JOIN EVM emite para "evm" (nunca "bsc" -- ver
backend/chain/auth_networks.py:EVM_AUTH_ONLY_LIMITATIONS para el porque).
Este archivo se mantiene enfocado en JOIN (tron, heredado); ver
test_join_evm_command.py para el equivalente EVM.
"""

import re

from backend import timeutils
from backend.chain import reset_chain_adapter_for_tests, set_chain_adapter_for_tests
from backend.chain.tron_adapter import TronEscrowAdapter
from backend.config import settings
from backend.messaging import identity
from backend.messaging.router import ConversationRouter
from backend.messaging.whatsapp_adapter import SandboxMetaClient, WhatsAppAdapter

import pytest

tronpy = pytest.importorskip("tronpy", reason="tronpy not installed in this environment")

from tronpy.keys import PrivateKey  # noqa: E402

from .conftest import auth_headers  # noqa: E402


@pytest.fixture()
def real_tron_adapter_for_tests():
    adapter = TronEscrowAdapter.__new__(TronEscrowAdapter)
    set_chain_adapter_for_tests(adapter)
    yield adapter
    reset_chain_adapter_for_tests()


def _extract_link(text: str) -> tuple[str, str]:
    match = re.search(r"[?&]token=([^&\s]+)&network=([^&\s]+)", text)
    assert match, f"no se encontro token/network en el mensaje de JOIN: {text!r}"
    return match.group(1), match.group(2)


def test_join_token_network_authenticated_network_and_linked_wallet_all_agree(client, db_session, real_tron_adapter_for_tests):
    whatsapp_id = "wa-join-flow-consistency-1"
    pk = PrivateKey.random()
    address = pk.public_key.to_base58check_address()

    # --- JOIN real, via el router, no un atajo de test ---
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, whatsapp_id, "JOIN")

    to, text = mock.sent[-1]
    assert to == whatsapp_id
    join_token, join_network = _extract_link(text)

    # Hallazgo documentado (ver docstring del modulo): JOIN hoy SIEMPRE
    # emite para settings.CHAIN_ADAPTER -- "tron".
    assert join_network == settings.CHAIN_ADAPTER == "tron"

    # --- /auth/nonce -> firma real -> /auth/verify ---
    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address, "network_id": join_network})
    assert nonce_resp.status_code == 200, nonce_resp.text
    message = nonce_resp.json()["message"]
    signature = pk.sign_msg(message.encode()).hex()

    verify_resp = client.post(
        "/auth/verify", json={"wallet_address": address, "signature": signature, "network_id": join_network}
    )
    assert verify_resp.status_code == 200, verify_resp.text
    access_token = verify_resp.json()["access_token"]

    # --- la red que el JWT realmente prueba coincide con la del token JOIN ---
    from jose import jwt as jose_jwt

    decoded = jose_jwt.decode(access_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    authenticated_network = decoded["network_id"]
    assert authenticated_network == join_network == "tron"

    # --- /whatsapp/link con el JWT real y el token real de JOIN ---
    link_resp = client.post(
        "/whatsapp/link",
        json={"link_token": join_token, "network_id": join_network},
        headers=auth_headers(access_token),
    )
    assert link_resp.status_code == 200, link_resp.text
    assert link_resp.json() == {"whatsapp_id": whatsapp_id, "linked": True}

    # --- la wallet vinculada es EXACTAMENTE la que firmo, en la MISMA red ---
    linked_user = identity.get_linked_user(db_session, whatsapp_id)
    assert linked_user is not None
    assert linked_user.wallet_address == address
    assert linked_user.network_id == authenticated_network == join_network


def test_join_link_token_is_scoped_to_the_network_it_was_issued_for_end_to_end(client, db_session, real_tron_adapter_for_tests):
    """Complemento del test anterior: si alguien intentara reusar el MISMO
    link_token de JOIN afirmando una red distinta, las tres verificaciones
    (autenticada / del token JOIN / body del request) dejan de coincidir y
    el flujo se rechaza -- no solo en teoria, sino siguiendo el token real
    que JOIN produjo."""
    whatsapp_id = "wa-join-flow-consistency-2"
    pk = PrivateKey.random()
    address = pk.public_key.to_base58check_address()

    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, whatsapp_id, "JOIN")
    to, text = mock.sent[-1]
    join_token, join_network = _extract_link(text)
    assert join_network == "tron"

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address, "network_id": "tron"})
    message = nonce_resp.json()["message"]
    signature = pk.sign_msg(message.encode()).hex()
    verify_resp = client.post("/auth/verify", json={"wallet_address": address, "signature": signature, "network_id": "tron"})
    access_token = verify_resp.json()["access_token"]

    resp = client.post(
        "/whatsapp/link",
        json={"link_token": join_token, "network_id": "evm"},  # afirma una red que el token de JOIN NUNCA tuvo
        headers=auth_headers(access_token),
    )
    assert resp.status_code == 400
    assert "network mismatch" in resp.json()["detail"]
    assert identity.get_linked_user(db_session, whatsapp_id) is None
