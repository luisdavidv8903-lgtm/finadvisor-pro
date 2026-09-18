"""
Visibilidad: PUBLIC no ve nada, MEMBER ve campos permitidos, MATCHED_COUNTERPARTY
y DISPUTE_ARBITRATOR ven datos de liquidacion solo de SU trade, y solo cuando
corresponde. Tambien prueba que ningun endpoint filtra un objeto ORM crudo.
"""

from backend.models import MemberRole, P2POrderDB

from .conftest import auth_headers, login, seed_membership, sync_indexer

SELLER = "TFakeSeller11111111111111111111"
BUYER = "TFakeBuyer111111111111111111111"
ARBITER = "TFakeArbiter1111111111111111111"
OTHER_MEMBER = "TFakeOtherMember1111111111111111"
OTHER_BUYER = "TFakeOtherBuyer11111111111111111"


def _seed_order(db_session, fake_chain, onchain_id, *, seller=SELLER, buyer=BUYER, arbiter=ARBITER, amount=100_000000, disputed=False):
    fake_chain.seed_order_created(onchain_id, seller, "FAKE_TOKEN", amount)
    fake_chain.seed_order_claimed(onchain_id, buyer, arbiter)
    fake_chain.seed_paid(onchain_id)
    if disputed:
        fake_chain.seed_disputed(onchain_id)
    sync_indexer(db_session, fake_chain)
    return db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()


def _attach_settlement(client, db_session, order, seller_wallet, seller_token):
    create = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_holder": "Synthetic Test", "account_number": "0000-TEST"}},
        headers=auth_headers(seller_token),
    )
    assert create.status_code == 201, create.text
    detail_id = create.json()["id"]

    metadata = client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": order.onchain_order_id,
            "fiat_amount": "1000.00",
            "fiat_currency": "CUP",
            "payment_method": "Transferencia CUP",
            "settlement_detail_id": detail_id,
        },
        headers=auth_headers(seller_token),
    )
    assert metadata.status_code == 201, metadata.text
    return detail_id


def _member(client, db_session, wallet):
    seed_membership(db_session, wallet, role=MemberRole.MEMBER)
    return login(client, wallet)


# --- PUBLIC ---


def test_unauthenticated_cannot_list_orders(client):
    r = client.get("/orders/")
    assert r.status_code == 401


def test_unauthenticated_root_reveals_nothing_marketplace_specific(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert "order" not in str(body).lower()
    assert "wallet" not in str(body).lower()


# --- MEMBER ---


def test_authenticated_non_member_denied_order_book(client, db_session):
    token = login(client, "TFakeNotAMember0000000000000001")
    r = client.get("/orders/", headers=auth_headers(token))
    assert r.status_code == 403


def test_member_sees_allowed_fields_only(client, db_session, fake_chain):
    order = _seed_order(db_session, fake_chain, 1)
    token = _member(client, db_session, OTHER_MEMBER)

    r = client.get(f"/orders/{order.id}", headers=auth_headers(token))
    assert r.status_code == 200
    body = r.json()

    expected_keys = {
        "id",
        "onchain_status",
        "seller_wallet_masked",
        "buyer_wallet_masked",
        "crypto_amount",
        "fiat_amount",
        "fiat_currency",
        "payment_method",
        "quoted_rate",
        "created_at",
    }
    assert set(body.keys()) == expected_keys, "no raw ORM fields / no extra fields must leak"

    assert body["seller_wallet_masked"] != SELLER
    assert SELLER not in str(body)  # la wallet completa nunca aparece en la respuesta
    assert "…" in body["seller_wallet_masked"]


def test_member_cannot_see_settlement_details_of_unrelated_order(client, db_session, fake_chain):
    order = _seed_order(db_session, fake_chain, 1)
    seller_token = _member(client, db_session, SELLER)
    _attach_settlement(client, db_session, order, SELLER, seller_token)

    outsider_token = _member(client, db_session, OTHER_MEMBER)
    r = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(outsider_token))
    assert r.status_code == 403


# --- MATCHED_COUNTERPARTY ---


def test_matched_counterparty_can_see_only_own_trade_settlement(client, db_session, fake_chain):
    order = _seed_order(db_session, fake_chain, 1)
    seller_token = _member(client, db_session, SELLER)
    _attach_settlement(client, db_session, order, SELLER, seller_token)

    buyer_token = _member(client, db_session, BUYER)
    own_trade = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(buyer_token))
    assert own_trade.status_code == 200
    assert own_trade.json()["payload"]["account_holder"] == "Synthetic Test"

    # una SEGUNDA orden, distinta contraparte — el mismo buyer no deberia poder verla
    order2 = _seed_order(db_session, fake_chain, 2, seller=SELLER, buyer=OTHER_BUYER)
    _attach_settlement(client, db_session, order2, SELLER, seller_token)

    unrelated = client.get(f"/orders/{order2.id}/settlement", headers=auth_headers(buyer_token))
    assert unrelated.status_code == 403


# --- DISPUTE_ARBITRATOR ---


def test_arbitrator_cannot_see_non_disputed_order_details(client, db_session, fake_chain):
    order = _seed_order(db_session, fake_chain, 1, disputed=False)  # PAID, no DISPUTED todavia
    seller_token = _member(client, db_session, SELLER)
    _attach_settlement(client, db_session, order, SELLER, seller_token)

    arbiter_token = _member(client, db_session, ARBITER)
    r = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(arbiter_token))
    assert r.status_code == 403


def test_arbitrator_can_access_authorized_disputed_trade_only(client, db_session, fake_chain):
    disputed_order = _seed_order(db_session, fake_chain, 1, disputed=True)
    seller_token = _member(client, db_session, SELLER)
    _attach_settlement(client, db_session, disputed_order, SELLER, seller_token)

    arbiter_token = _member(client, db_session, ARBITER)
    authorized = client.get(f"/orders/{disputed_order.id}/settlement", headers=auth_headers(arbiter_token))
    assert authorized.status_code == 200

    # una SEGUNDA orden disputada, pero con OTRO arbiter_snapshot — el primer
    # arbitro no es parte de esta disputa y no deberia poder verla
    other_disputed = _seed_order(db_session, fake_chain, 2, seller=SELLER, buyer=OTHER_BUYER, arbiter="TFakeOtherArbiter00000000000001", disputed=True)
    _attach_settlement(client, db_session, other_disputed, SELLER, seller_token)

    unauthorized = client.get(f"/orders/{other_disputed.id}/settlement", headers=auth_headers(arbiter_token))
    assert unauthorized.status_code == 403


def test_raw_orm_fields_cannot_leak_through_order_list(client, db_session, fake_chain):
    _seed_order(db_session, fake_chain, 1)
    token = _member(client, db_session, OTHER_MEMBER)
    r = client.get("/orders/", headers=auth_headers(token))
    assert r.status_code == 200
    for item in r.json():
        assert "seller_wallet" not in item  # solo la version *_masked
        assert "settlement_detail_id" not in item
        assert "onchain_order_id" not in item
