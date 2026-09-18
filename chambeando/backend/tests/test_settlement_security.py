"""Settlement details: cifrado en reposo, autorizacion, revocacion, logging."""

from backend.models import MemberRole, P2POrderDB, SecurityEventDB, SecurityEventType, SettlementDetailDB
from backend.security.crypto import decrypt_settlement_payload, encrypt_settlement_payload

from .conftest import auth_headers, login, seed_membership, sync_indexer

SELLER = "TFakeSeller22222222222222222222"
BUYER = "TFakeBuyer222222222222222222222"
ARBITER = "TFakeArbiter2222222222222222222"
OUTSIDER = "TFakeOutsider2222222222222222222"

SENSITIVE_ACCOUNT_NUMBER = "SYNTHETIC-ACCT-9988776655"


def _member(client, db_session, wallet):
    seed_membership(db_session, wallet, role=MemberRole.MEMBER)
    return login(client, wallet)


def _seed_disputed_order(db_session, fake_chain, onchain_id=1):
    fake_chain.seed_order_created(onchain_id, SELLER, "FAKE_TOKEN", 100_000000)
    fake_chain.seed_order_claimed(onchain_id, BUYER, ARBITER)
    fake_chain.seed_paid(onchain_id)
    fake_chain.seed_disputed(onchain_id)
    sync_indexer(db_session, fake_chain)
    return db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()


def test_encrypted_at_rest_and_plaintext_absent_from_db_column(client, db_session):
    token = _member(client, db_session, SELLER)
    r = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": SENSITIVE_ACCOUNT_NUMBER}},
        headers=auth_headers(token),
    )
    assert r.status_code == 201
    detail = db_session.query(SettlementDetailDB).filter(SettlementDetailDB.id == r.json()["id"]).first()
    assert isinstance(detail.encrypted_payload, (bytes, bytearray))
    assert SENSITIVE_ACCOUNT_NUMBER.encode() not in bytes(detail.encrypted_payload)
    assert SENSITIVE_ACCOUNT_NUMBER not in str(detail.encrypted_payload)


def test_decrypt_roundtrip_matches_original():
    payload = {"account_number": SENSITIVE_ACCOUNT_NUMBER, "bank": "Synthetic Bank"}
    ciphertext = encrypt_settlement_payload(payload)
    assert SENSITIVE_ACCOUNT_NUMBER.encode() not in ciphertext
    assert decrypt_settlement_payload(ciphertext) == payload


def test_decrypted_only_for_authorized_context(client, db_session, fake_chain):
    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    create = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": SENSITIVE_ACCOUNT_NUMBER}},
        headers=auth_headers(seller_token),
    )
    detail_id = create.json()["id"]
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order.onchain_order_id, "fiat_amount": "1000.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": detail_id},
        headers=auth_headers(seller_token),
    )

    # listado propio del owner NUNCA incluye el payload, ni siquiera al dueno
    own_list = client.get("/settlement-details", headers=auth_headers(seller_token))
    assert own_list.status_code == 200
    assert all("payload" not in item for item in own_list.json())
    assert SENSITIVE_ACCOUNT_NUMBER not in str(own_list.json())


def test_unrelated_member_denied_decrypted_payload(client, db_session, fake_chain):
    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    create = client.post(
        "/settlement-details", json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": SENSITIVE_ACCOUNT_NUMBER}}, headers=auth_headers(seller_token)
    )
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order.onchain_order_id, "fiat_amount": "1000.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": create.json()["id"]},
        headers=auth_headers(seller_token),
    )

    outsider_token = _member(client, db_session, OUTSIDER)
    r = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(outsider_token))
    assert r.status_code == 403
    assert SENSITIVE_ACCOUNT_NUMBER not in r.text


def test_matched_counterparty_authorized_only_for_own_trade(client, db_session, fake_chain):
    order1 = _seed_disputed_order(db_session, fake_chain, onchain_id=1)
    seller_token = _member(client, db_session, SELLER)
    d1 = client.post("/settlement-details", json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": "ACCT-ONE"}}, headers=auth_headers(seller_token)).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order1.onchain_order_id, "fiat_amount": "1000.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": d1["id"]},
        headers=auth_headers(seller_token),
    )

    buyer_token = _member(client, db_session, BUYER)
    ok = client.get(f"/orders/{order1.id}/settlement", headers=auth_headers(buyer_token))
    assert ok.status_code == 200
    assert ok.json()["payload"]["account_number"] == "ACCT-ONE"


def test_arbitrator_authorized_only_when_disputed(client, db_session, fake_chain):
    from backend.models import OrderStatus

    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    d = client.post("/settlement-details", json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": SENSITIVE_ACCOUNT_NUMBER}}, headers=auth_headers(seller_token)).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order.onchain_order_id, "fiat_amount": "1000.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": d["id"]},
        headers=auth_headers(seller_token),
    )

    arbiter_token = _member(client, db_session, ARBITER)
    while_disputed = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(arbiter_token))
    assert while_disputed.status_code == 200

    # la disputa se resuelve -> ya no esta DISPUTED -> el ex-arbitro pierde acceso
    order.onchain_status = OrderStatus.RELEASED
    db_session.commit()

    after_resolution = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(arbiter_token))
    assert after_resolution.status_code == 403


def test_sensitive_strings_absent_from_logs(client, db_session, fake_chain, caplog):
    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    d = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": SENSITIVE_ACCOUNT_NUMBER, "zelle": "synthetic@example.test"}},
        headers=auth_headers(seller_token),
    ).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order.onchain_order_id, "fiat_amount": "1000.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": d["id"]},
        headers=auth_headers(seller_token),
    )
    buyer_token = _member(client, db_session, BUYER)
    client.get(f"/orders/{order.id}/settlement", headers=auth_headers(buyer_token))

    # el propio log de auditoria (DB) nunca contiene el valor sensible
    events = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.SETTLEMENT_DETAIL_VIEWED).all()
    assert len(events) >= 1
    for e in events:
        assert SENSITIVE_ACCOUNT_NUMBER not in (e.reason or "")
        assert "synthetic@example.test" not in (e.reason or "")

    # y el logging de aplicacion (stdlib logging) tampoco
    assert SENSITIVE_ACCOUNT_NUMBER not in caplog.text
    assert "synthetic@example.test" not in caplog.text


def test_revocation_respected_going_forward_but_does_not_break_an_already_snapshotted_trade(client, db_session, fake_chain):
    """Corregido en Phase 2B.2 (ver SETTLEMENT_LIFECYCLE.md): revocar el
    registro maestro DESPUES de que una orden ya tomo su snapshot NO le quita
    acceso a la contraparte de ESA orden — esa es la propiedad de seguridad
    pedida ("once MATCHED, las instrucciones no cambian silenciosamente bajo
    la contraparte"). "Revocation respected" significa que el registro
    revocado ya no puede usarse para adjuntar a una orden NUEVA — ver el
    segundo test de este archivo, test_revoked_detail_cannot_be_linked_to_a_new_order."""
    order = _seed_disputed_order(db_session, fake_chain)
    seller_token = _member(client, db_session, SELLER)
    d = client.post("/settlement-details", json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": SENSITIVE_ACCOUNT_NUMBER}}, headers=auth_headers(seller_token)).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order.onchain_order_id, "fiat_amount": "1000.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": d["id"]},
        headers=auth_headers(seller_token),
    )

    revoke = client.post(f"/settlement-details/{d['id']}/revoke", headers=auth_headers(seller_token))
    assert revoke.status_code == 200
    assert revoke.json()["active"] is False

    buyer_token = _member(client, db_session, BUYER)
    r = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(buyer_token))
    assert r.status_code == 200  # el snapshot de ESTA orden sigue disponible pese a la revocacion del maestro
    assert r.json()["payload"]["account_number"] == SENSITIVE_ACCOUNT_NUMBER


def test_revoked_detail_cannot_be_linked_to_a_new_order(client, db_session, fake_chain):
    seller_token = _member(client, db_session, SELLER)
    d = client.post(
        "/settlement-details", json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": SENSITIVE_ACCOUNT_NUMBER}}, headers=auth_headers(seller_token)
    ).json()
    client.post(f"/settlement-details/{d['id']}/revoke", headers=auth_headers(seller_token))

    order = _seed_disputed_order(db_session, fake_chain, onchain_id=2)
    attach = client.post(
        "/orders/metadata",
        json={"onchain_order_id": order.onchain_order_id, "fiat_amount": "1000.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": d["id"]},
        headers=auth_headers(seller_token),
    )
    assert attach.status_code == 400  # un detail revocado no puede ser la fuente de un snapshot nuevo
