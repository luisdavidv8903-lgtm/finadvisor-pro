"""
Phase 2B.2 #5 — settlement detail lifecycle after MATCH. See
SETTLEMENT_LIFECYCLE.md for the explicit answers to the four questions posed.
"""

from backend.models import MemberRole, P2POrderDB

from .conftest import auth_headers, login, seed_membership, sync_indexer

SELLER = "TFakeLifecycleSeller00000000001"
BUYER = "TFakeLifecycleBuyer000000000001"
BUYER2 = "TFakeLifecycleBuyer200000000001"
ARBITER = "TFakeLifecycleArbiter0000000001"


def _member(client, db_session, wallet):
    seed_membership(db_session, wallet, role=MemberRole.MEMBER)
    return login(client, wallet)


def _seed_matched_order(db_session, fake_chain, onchain_id, *, seller=SELLER, buyer=BUYER):
    fake_chain.seed_order_created(onchain_id, seller, "TOKEN", 100_000000)
    fake_chain.seed_order_claimed(onchain_id, buyer, ARBITER)
    sync_indexer(db_session, fake_chain)
    return db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()


def test_post_match_revocation_does_not_remove_counterparty_access(client, db_session, fake_chain):
    order = _seed_matched_order(db_session, fake_chain, 1)
    seller_token = _member(client, db_session, SELLER)
    detail = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": "SYN-LIFECYCLE-1"}},
        headers=auth_headers(seller_token),
    ).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order.onchain_order_id, "fiat_amount": "10.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": detail["id"]},
        headers=auth_headers(seller_token),
    )

    client.post(f"/settlement-details/{detail['id']}/revoke", headers=auth_headers(seller_token))

    buyer_token = _member(client, db_session, BUYER)
    r = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(buyer_token))
    assert r.status_code == 200
    assert r.json()["payload"]["account_number"] == "SYN-LIFECYCLE-1"


def test_attempted_post_match_replacement_is_rejected(client, db_session, fake_chain):
    """Una vez que la orden ya tiene metadata (y por lo tanto un snapshot), un
    segundo intento de attach_order_metadata — con OTRO settlement_detail_id —
    se rechaza entero, y el snapshot original queda intacto."""
    order = _seed_matched_order(db_session, fake_chain, 1)
    seller_token = _member(client, db_session, SELLER)
    original = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": "SYN-ORIGINAL"}},
        headers=auth_headers(seller_token),
    ).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order.onchain_order_id, "fiat_amount": "10.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": original["id"]},
        headers=auth_headers(seller_token),
    )

    replacement = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": "SYN-ATTACKER-SWAP"}},
        headers=auth_headers(seller_token),
    ).json()
    attempt = client.post(
        "/orders/metadata",
        json={"onchain_order_id": order.onchain_order_id, "fiat_amount": "999.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": replacement["id"]},
        headers=auth_headers(seller_token),
    )
    assert attempt.status_code == 400  # "ya tiene metadata adjunta"

    buyer_token = _member(client, db_session, BUYER)
    r = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(buyer_token))
    assert r.status_code == 200
    assert r.json()["payload"]["account_number"] == "SYN-ORIGINAL"  # nunca cambio


def test_unrelated_trade_isolation(client, db_session, fake_chain):
    """Revocar/crear settlement details para UN trade no afecta en absoluto los
    snapshots de OTRO trade distinto del mismo vendedor."""
    order1 = _seed_matched_order(db_session, fake_chain, 1, buyer=BUYER)
    order2 = _seed_matched_order(db_session, fake_chain, 2, buyer=BUYER2)
    seller_token = _member(client, db_session, SELLER)

    d1 = client.post(
        "/settlement-details", json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": "SYN-TRADE-ONE"}}, headers=auth_headers(seller_token)
    ).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order1.onchain_order_id, "fiat_amount": "10.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": d1["id"]},
        headers=auth_headers(seller_token),
    )

    d2 = client.post(
        "/settlement-details", json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": "SYN-TRADE-TWO"}}, headers=auth_headers(seller_token)
    ).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": order2.onchain_order_id, "fiat_amount": "20.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": d2["id"]},
        headers=auth_headers(seller_token),
    )

    # revocar el detail del trade 1 no debe afectar el trade 2 en absoluto
    client.post(f"/settlement-details/{d1['id']}/revoke", headers=auth_headers(seller_token))

    buyer1_token = _member(client, db_session, BUYER)
    buyer2_token = _member(client, db_session, BUYER2)

    r1 = client.get(f"/orders/{order1.id}/settlement", headers=auth_headers(buyer1_token))
    r2 = client.get(f"/orders/{order2.id}/settlement", headers=auth_headers(buyer2_token))
    assert r1.status_code == 200 and r1.json()["payload"]["account_number"] == "SYN-TRADE-ONE"
    assert r2.status_code == 200 and r2.json()["payload"]["account_number"] == "SYN-TRADE-TWO"

    # y buyer1 no puede ver el settlement del trade 2 (aislamiento de acceso, no solo de contenido)
    cross = client.get(f"/orders/{order2.id}/settlement", headers=auth_headers(buyer1_token))
    assert cross.status_code == 403


def test_settlement_payload_immutable_once_snapshotted_even_if_master_record_survives(client, db_session, fake_chain):
    """No existe ningun endpoint de UPDATE sobre SettlementDetailDB (solo
    create/list/revoke) — el payload del registro maestro tampoco puede
    editarse in-place. Combinado con el snapshot write-once, esto confirma
    que las instrucciones de liquidacion de un trade ya adjuntado son
    verdaderamente inmutables durante toda la vida de ese trade."""
    import inspect

    from backend.routers import settlement as settlement_router

    source = inspect.getsource(settlement_router)
    assert "@router.put(" not in source
    assert "@router.patch(" not in source
    assert ".encrypted_payload =" not in source  # nunca se reasigna in-place, solo se crea nuevo
