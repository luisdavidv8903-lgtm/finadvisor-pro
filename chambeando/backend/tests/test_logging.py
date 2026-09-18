"""
Logging: eventos requeridos se registran, y ningun string sensible sintetico
aparece jamas en el log de auditoria (DB) ni en el logging de aplicacion
(stdlib `logging`, capturado por `caplog`)."""

from backend.models import MemberRole, P2POrderDB, SecurityEventDB, SecurityEventType

from .conftest import auth_headers, login, seed_membership, sync_indexer
from .fake_chain_adapter import make_valid_signature

SENSITIVE_BANK = "SYNTHETIC-BANK-ACCT-1234567890"
SENSITIVE_CARD = "4111-1111-1111-1111"
SENSITIVE_ZELLE = "synthetic-zelle@example.test"

SELLER = "TFakeLogSeller00000000000000001"
BUYER = "TFakeLogBuyer000000000000000001"
ARBITER = "TFakeLogArbiter0000000000000001"


def _all_sensitive_absent(haystack: str) -> None:
    for secret in (SENSITIVE_BANK, SENSITIVE_CARD, SENSITIVE_ZELLE):
        assert secret not in haystack


def test_auth_failure_logged_without_secret_leakage(client, db_session, caplog):
    nonce = client.post("/auth/nonce", json={"wallet_address": SELLER}).json()
    bogus_signature = f"bogus-signature-containing-{SENSITIVE_CARD}"
    client.post("/auth/verify", json={"wallet_address": SELLER, "signature": bogus_signature})

    events = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.AUTH_FAILURE).all()
    assert len(events) == 1
    assert SENSITIVE_CARD not in (events[0].reason or "")
    assert bogus_signature not in (events[0].reason or "")  # ni siquiera la firma cruda se guarda

    _all_sensitive_absent(caplog.text)


def test_invite_redemption_logged(client, db_session):
    admin = seed_membership(db_session, "TFakeLogAdmin00000000000000001", role=MemberRole.ADMIN)
    admin_token = login(client, "TFakeLogAdmin00000000000000001")
    invite = client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 24}, headers=auth_headers(admin_token)).json()

    member_token = login(client, "TFakeLogNewMember000000000001")
    client.post("/invites/redeem", json={"code": invite["code"]}, headers=auth_headers(member_token))

    events = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.INVITE_REDEEMED).all()
    assert len(events) == 1
    assert invite["code"] not in (events[0].target_id or "") + (events[0].reason or "")  # el codigo nunca se loguea


def test_settlement_detail_access_logged_without_value(client, db_session, fake_chain, caplog):
    fake_chain.seed_order_created(1, SELLER, "TOKEN", 100_000000)
    fake_chain.seed_order_claimed(1, BUYER, ARBITER)
    fake_chain.seed_paid(1)
    fake_chain.seed_disputed(1)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    seed_membership(db_session, SELLER, role=MemberRole.MEMBER)
    seller_token = login(client, SELLER)
    detail = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": SENSITIVE_BANK, "zelle": SENSITIVE_ZELLE}},
        headers=auth_headers(seller_token),
    ).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": 1, "fiat_amount": "10.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": detail["id"]},
        headers=auth_headers(seller_token),
    )

    seed_membership(db_session, BUYER, role=MemberRole.MEMBER)
    buyer_token = login(client, BUYER)
    client.get(f"/orders/{order.id}/settlement", headers=auth_headers(buyer_token))

    events = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.SETTLEMENT_DETAIL_VIEWED).all()
    assert len(events) == 1
    assert events[0].target_id == str(order.id)
    assert events[0].reason is None or (SENSITIVE_BANK not in events[0].reason and SENSITIVE_ZELLE not in events[0].reason)
    _all_sensitive_absent(caplog.text)


def test_dispute_evidence_access_logged_without_content(client, db_session, fake_chain, caplog):
    fake_chain.seed_order_created(1, SELLER, "TOKEN", 100_000000)
    fake_chain.seed_order_claimed(1, BUYER, ARBITER)
    fake_chain.seed_paid(1)
    fake_chain.seed_disputed(1)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    seed_membership(db_session, SELLER, role=MemberRole.MEMBER)
    seller_token = login(client, SELLER)
    sensitive_note = f"my bank account is {SENSITIVE_BANK}"
    client.post(
        "/disputes/evidence", json={"order_id": order.id, "evidence_type": "note", "note": sensitive_note}, headers=auth_headers(seller_token)
    )

    seed_membership(db_session, BUYER, role=MemberRole.MEMBER)
    buyer_token = login(client, BUYER)
    client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(buyer_token))

    events = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.DISPUTE_EVIDENCE_VIEWED).all()
    assert len(events) >= 1
    for e in events:
        assert e.reason is None or SENSITIVE_BANK not in e.reason
    # el log de auditoria referencia la orden, NUNCA el contenido de la nota
    assert all((e.reason is None or sensitive_note not in e.reason) for e in events)
    _all_sensitive_absent(caplog.text)


def test_role_and_suspension_changes_logged(client, db_session):
    admin = seed_membership(db_session, "TFakeLogAdmin20000000000000001", role=MemberRole.ADMIN)
    target = seed_membership(db_session, "TFakeLogTarget0000000000000001", role=MemberRole.MEMBER)
    admin_token = login(client, "TFakeLogAdmin20000000000000001")

    client.post(f"/admin/memberships/{target.id}/role", json={"role": "moderator"}, headers=auth_headers(admin_token))
    client.post(f"/admin/memberships/{target.id}/suspend", json={"reason": "synthetic reason"}, headers=auth_headers(admin_token))

    role_events = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.ROLE_CHANGED).all()
    suspend_events = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.MEMBERSHIP_SUSPENDED).all()
    assert len(role_events) == 1
    assert len(suspend_events) == 1
    assert suspend_events[0].reason == "synthetic reason"


def test_synthetic_bank_card_zelle_strings_never_in_captured_logs(client, db_session, fake_chain, caplog):
    """Prueba integral: corre un flujo completo (settlement + evidencia + auth
    fallida) que a proposito incluye strings sinteticos de banco/tarjeta/Zelle,
    y verifica que NINGUNO aparezca en el logging de aplicacion capturado."""
    client.post("/auth/verify", json={"wallet_address": "TFakeIntegration00000000000001", "signature": f"invalid-{SENSITIVE_CARD}"})

    fake_chain.seed_order_created(1, SELLER, "TOKEN", 100_000000)
    fake_chain.seed_order_claimed(1, BUYER, ARBITER)
    fake_chain.seed_paid(1)
    fake_chain.seed_disputed(1)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    seed_membership(db_session, SELLER, role=MemberRole.MEMBER)
    seller_token = login(client, SELLER)
    detail = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": SENSITIVE_BANK, "card": SENSITIVE_CARD, "zelle": SENSITIVE_ZELLE}},
        headers=auth_headers(seller_token),
    ).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": 1, "fiat_amount": "10.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": detail["id"]},
        headers=auth_headers(seller_token),
    )
    client.post("/disputes/evidence", json={"order_id": order.id, "evidence_type": "note", "note": f"paid via {SENSITIVE_ZELLE}"}, headers=auth_headers(seller_token))

    seed_membership(db_session, BUYER, role=MemberRole.MEMBER)
    buyer_token = login(client, BUYER)
    client.get(f"/orders/{order.id}/settlement", headers=auth_headers(buyer_token))
    client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(buyer_token))

    _all_sensitive_absent(caplog.text)
