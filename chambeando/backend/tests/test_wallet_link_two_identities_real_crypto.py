"""
Fase de vinculacion segura (revision 2026-09-25) — pruebas con DOS identidades
reales (firma criptografica real via tronpy, NO el esquema trivial de
FakeChainAdapter) para el flujo completo /auth/nonce -> firma -> /auth/verify
-> /whatsapp/link. Cubre exactamente lo pedido para esta fase: wallets
distintas, vinculo de red, token expirado/reutilizado/intercambiado, y
rechazo de una segunda wallet para un WhatsApp ID ya vinculado.

Mismo patron que test_tron_signature_real.py: claves SINTETICAS generadas al
vuelo (PrivateKey.random()), nunca usadas en testnet/mainnet, sin valor real.
Se salta limpiamente si tronpy no esta instalado en el entorno.
"""

from datetime import timedelta

import pytest

tronpy = pytest.importorskip("tronpy", reason="tronpy not installed in this environment")

from tronpy.keys import PrivateKey  # noqa: E402

from backend import timeutils  # noqa: E402
from backend.chain import reset_chain_adapter_for_tests, set_chain_adapter_for_tests  # noqa: E402
from backend.chain.tron_adapter import TronEscrowAdapter  # noqa: E402
from backend.messaging import identity  # noqa: E402
from backend.models import WhatsAppLinkDB  # noqa: E402

from .conftest import auth_headers  # noqa: E402


def _fresh_keypair():
    pk = PrivateKey.random()
    return pk, pk.public_key.to_base58check_address()


@pytest.fixture()
def real_tron_adapter_for_tests():
    """Misma TronEscrowAdapter real de produccion, construida sin __init__
    (no requiere nodo/contrato) -- ver test_tron_signature_real.py."""
    adapter = TronEscrowAdapter.__new__(TronEscrowAdapter)
    set_chain_adapter_for_tests(adapter)
    yield adapter
    reset_chain_adapter_for_tests()


def _real_login(client, pk, address) -> str:
    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address})
    assert nonce_resp.status_code == 200, nonce_resp.text
    message = nonce_resp.json()["message"]
    signature = pk.sign_msg(message.encode()).hex()
    verify_resp = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    assert verify_resp.status_code == 200, verify_resp.text
    return verify_resp.json()["access_token"]


def test_two_distinct_real_wallets_link_to_two_distinct_whatsapp_ids(client, db_session, real_tron_adapter_for_tests):
    pk_a, address_a = _fresh_keypair()
    pk_b, address_b = _fresh_keypair()

    token_a = identity.issue_link_token(db_session, "wa-real-a", 600, network_id="tron")
    token_b = identity.issue_link_token(db_session, "wa-real-b", 600, network_id="tron")

    jwt_a = _real_login(client, pk_a, address_a)
    jwt_b = _real_login(client, pk_b, address_b)

    resp_a = client.post("/whatsapp/link", json={"link_token": token_a, "network_id": "tron"}, headers=auth_headers(jwt_a))
    assert resp_a.status_code == 200, resp_a.text
    assert resp_a.json() == {"whatsapp_id": "wa-real-a", "linked": True}

    resp_b = client.post("/whatsapp/link", json={"link_token": token_b, "network_id": "tron"}, headers=auth_headers(jwt_b))
    assert resp_b.status_code == 200, resp_b.text
    assert resp_b.json() == {"whatsapp_id": "wa-real-b", "linked": True}

    user_a = identity.get_linked_user(db_session, "wa-real-a")
    user_b = identity.get_linked_user(db_session, "wa-real-b")
    assert user_a.wallet_address == address_a
    assert user_b.wallet_address == address_b
    assert user_a.id != user_b.id


def test_network_binding_rejects_mismatched_network_with_real_crypto(client, db_session, real_tron_adapter_for_tests):
    pk, address = _fresh_keypair()
    token = identity.issue_link_token(db_session, "wa-real-network-mismatch", 600, network_id="tron")
    jwt = _real_login(client, pk, address)

    resp = client.post("/whatsapp/link", json={"link_token": token, "network_id": "bsc"}, headers=auth_headers(jwt))
    assert resp.status_code == 400
    assert "network mismatch" in resp.json()["detail"]
    assert identity.get_linked_user(db_session, "wa-real-network-mismatch") is None


def test_expired_token_rejected_with_real_crypto(client, db_session, real_tron_adapter_for_tests):
    pk, address = _fresh_keypair()
    token = identity.issue_link_token(db_session, "wa-real-expired", 600, network_id="tron")

    link_row = db_session.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.whatsapp_id == "wa-real-expired").first()
    link_row.link_token_expires_at = timeutils.utcnow() - timedelta(seconds=10)
    db_session.commit()

    jwt = _real_login(client, pk, address)
    resp = client.post("/whatsapp/link", json={"link_token": token, "network_id": "tron"}, headers=auth_headers(jwt))
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"]


def test_reused_token_rejected_with_real_crypto(client, db_session, real_tron_adapter_for_tests):
    pk, address = _fresh_keypair()
    token = identity.issue_link_token(db_session, "wa-real-reused", 600, network_id="tron")

    jwt_first = _real_login(client, pk, address)
    first = client.post("/whatsapp/link", json={"link_token": token, "network_id": "tron"}, headers=auth_headers(jwt_first))
    assert first.status_code == 200, first.text

    # nueva sesion/JWT (el nonce es de un solo uso) pero el MISMO link_token ya consumido
    jwt_second = _real_login(client, pk, address)
    second = client.post("/whatsapp/link", json={"link_token": token, "network_id": "tron"}, headers=auth_headers(jwt_second))
    assert second.status_code == 400
    assert "invalid link token" in second.json()["detail"]


def test_swapped_token_between_two_real_identities_links_the_token_issuers_whatsapp_id(client, db_session, real_tron_adapter_for_tests):
    """El token pertenece al WhatsApp ID para el que fue emitido, no a quien
    lo tipeo con JOIN -- si el token de A termina en manos de la wallet de B,
    el resultado es que el WhatsApp ID de A queda vinculado a la wallet de B
    (y viceversa). Esto es inherente a cualquier esquema de token portador de
    un solo uso: la propiedad que protege el sistema es "quien tiene el token
    valido, no reutilizado, de la red correcta", no "la misma persona que
    disparo JOIN". Documentado aqui a proposito -- ver bundle de revision,
    seccion de riesgos pendientes."""
    pk_a, address_a = _fresh_keypair()
    pk_b, address_b = _fresh_keypair()

    token_a = identity.issue_link_token(db_session, "wa-real-swap-a", 600, network_id="tron")
    token_b = identity.issue_link_token(db_session, "wa-real-swap-b", 600, network_id="tron")

    jwt_a = _real_login(client, pk_a, address_a)
    jwt_b = _real_login(client, pk_b, address_b)

    # B consume el token de A (intercambiado)
    resp = client.post("/whatsapp/link", json={"link_token": token_a, "network_id": "tron"}, headers=auth_headers(jwt_b))
    assert resp.status_code == 200, resp.text
    assert resp.json()["whatsapp_id"] == "wa-real-swap-a"

    # A consume el token de B (intercambiado tambien)
    resp2 = client.post("/whatsapp/link", json={"link_token": token_b, "network_id": "tron"}, headers=auth_headers(jwt_a))
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["whatsapp_id"] == "wa-real-swap-b"

    assert identity.get_linked_user(db_session, "wa-real-swap-a").wallet_address == address_b
    assert identity.get_linked_user(db_session, "wa-real-swap-b").wallet_address == address_a


def test_second_distinct_wallet_rejected_for_already_linked_whatsapp_id(client, db_session, real_tron_adapter_for_tests):
    pk_a, address_a = _fresh_keypair()
    pk_c, address_c = _fresh_keypair()

    token_1 = identity.issue_link_token(db_session, "wa-real-already-linked", 600, network_id="tron")
    jwt_a = _real_login(client, pk_a, address_a)
    first = client.post("/whatsapp/link", json={"link_token": token_1, "network_id": "tron"}, headers=auth_headers(jwt_a))
    assert first.status_code == 200, first.text

    # una segunda wallet DISTINTA intenta vincularse al mismo WhatsApp ID via un nuevo token
    token_2 = identity.issue_link_token(db_session, "wa-real-already-linked", 600, network_id="tron")
    jwt_c = _real_login(client, pk_c, address_c)
    second = client.post("/whatsapp/link", json={"link_token": token_2, "network_id": "tron"}, headers=auth_headers(jwt_c))
    assert second.status_code == 400
    assert "already linked to a different wallet" in second.json()["detail"]
    assert identity.get_linked_user(db_session, "wa-real-already-linked").wallet_address == address_a

    # en cambio, la MISMA wallet re-vinculando su propio WhatsApp ID (p.ej. re-tipeo JOIN) si funciona
    token_3 = identity.issue_link_token(db_session, "wa-real-already-linked", 600, network_id="tron")
    jwt_a_again = _real_login(client, pk_a, address_a)
    third = client.post("/whatsapp/link", json={"link_token": token_3, "network_id": "tron"}, headers=auth_headers(jwt_a_again))
    assert third.status_code == 200, third.text
