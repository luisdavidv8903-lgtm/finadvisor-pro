"""Reputacion: contadores derivados, nunca fabricables ni editables via API."""

import inspect

from backend import schemas
from backend.models import MemberRole, P2POrderDB, UserDB

from .conftest import auth_headers, login, seed_membership, sync_indexer

SELLER = "TFakeRepSeller00000000000000001"
BUYER = "TFakeRepBuyer000000000000000001"
ARBITER = "TFakeRepArbiter0000000000000001"


def _member(client, db_session, wallet):
    seed_membership(db_session, wallet, role=MemberRole.MEMBER)
    return login(client, wallet)


def test_counters_derived_from_verifiable_trade_events_only(client, db_session, fake_chain):
    # orden 1: completada normalmente
    fake_chain.seed_order_created(1, SELLER, "TOKEN", 100)
    fake_chain.seed_order_claimed(1, BUYER, ARBITER)
    fake_chain.seed_paid(1)
    fake_chain.seed_settled(1, "RELEASED")
    # orden 2: cancelada antes de match
    fake_chain.seed_order_created(2, SELLER, "TOKEN", 50)
    fake_chain.seed_cancelled(2)
    # orden 3: disputada, el comprador gana
    fake_chain.seed_order_created(3, SELLER, "TOKEN", 200)
    fake_chain.seed_order_claimed(3, BUYER, ARBITER)
    fake_chain.seed_paid(3)
    fake_chain.seed_disputed(3)
    fake_chain.seed_settled(3, "RELEASED")
    sync_indexer(db_session, fake_chain)

    login(client, SELLER)  # crea la fila UserDB minima para la wallet vendedora (ver auth.py)
    seller_user = db_session.query(UserDB).filter(UserDB.wallet_address == SELLER).first()
    viewer_token = _member(client, db_session, "TFakeRepViewer0000000000000001")

    r = client.get(f"/reputation/{seller_user.id}", headers=auth_headers(viewer_token))
    assert r.status_code == 200
    body = r.json()
    assert body["completed_trades"] == 2  # ordenes 1 y 3 (RELEASED)
    assert body["cancelled_trades"] == 1  # orden 2
    assert body["disputes_opened"] == 1  # orden 3
    assert body["disputes_lost"] == 1  # seller perdio la disputa de la orden 3 (recipient=buyer)
    assert body["disputes_won"] == 0


def test_api_cannot_submit_its_own_completed_trade_count():
    """Ningun schema de creacion/edicion en todo el backend tiene un campo de
    contador de reputacion — es estructuralmente imposible enviarlo."""
    forbidden_fields = {"completed_trades", "cancelled_trades", "disputes_won", "disputes_lost", "reputation_score"}
    for name, obj in vars(schemas).items():
        if not inspect.isclass(obj) or not hasattr(obj, "model_fields"):
            continue
        if "Create" in name or "Request" in name:
            overlap = forbidden_fields & set(obj.model_fields)
            assert not overlap, f"{name} expone un campo de reputacion editable: {overlap}"


def test_admin_cannot_directly_edit_completed_trade_history(client, db_session, fake_chain):
    """No existe NINGUN endpoint bajo /admin que ESCRIBA P2POrderDB.onchain_status
    ni ningun dato usado para calcular reputacion. Desde Phase 2B.1, /admin SI
    tiene rutas con `order_id` en el path (gestion de DisputeAssignment), pero
    solo para LEER (validar que la orden existe y esta DISPUTED) — nunca para
    escribir estado on-chain ni contadores de reputacion. Se verifica que
    ninguna ruta de admin sea de reputacion, y que el codigo fuente nunca
    escriba `onchain_status` ni construya una fila de orden."""
    import inspect

    from backend.routers import admin as admin_router

    for route in admin_router.router.routes:
        path = getattr(route, "path", "")
        assert "reputation" not in path.lower()

    source = inspect.getsource(admin_router)
    assert "onchain_status =" not in source
    assert "P2POrderDB(" not in source


def test_dispute_counters_cannot_be_arbitrarily_forged(client, db_session, fake_chain):
    """was_disputed y onchain_status solo los escribe el indexer — no hay
    endpoint que permita marcar una orden como disputada sin el evento
    DisputeRaised real."""
    fake_chain.seed_order_created(1, SELLER, "TOKEN", 100)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()
    assert order.was_disputed is False

    token = _member(client, db_session, SELLER)
    # ni siquiera el propio vendedor tiene forma de setear was_disputed via /orders/metadata
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": 1, "fiat_amount": "10.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "was_disputed": True},
        headers=auth_headers(token),
    )
    db_session.refresh(order)
    assert order.was_disputed is False


def test_reputation_endpoint_requires_member_status(client, db_session):
    seed_membership(db_session, SELLER, role=MemberRole.MEMBER)
    seller_user = db_session.query(UserDB).filter(UserDB.wallet_address == SELLER).first()

    non_member_token = login(client, "TFakeNotMemberRep0000000000001")
    r = client.get(f"/reputation/{seller_user.id}", headers=auth_headers(non_member_token))
    assert r.status_code == 403

    unauthenticated = client.get(f"/reputation/{seller_user.id}")
    assert unauthenticated.status_code == 401
