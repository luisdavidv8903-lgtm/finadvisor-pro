"""Roles/privilegios: MEMBER < MODERATOR/ADMIN, sin jerarquia implicita, sin
'god mode' para ADMIN sobre fondos/estado on-chain/reputacion."""

from backend.models import MemberRole, MembershipDB, P2POrderDB, SecurityEventDB, SecurityEventType

from .conftest import auth_headers, login, seed_membership, sync_indexer

MEMBER_WALLET = "TFakeRoleMember0000000000000001"
MODERATOR_WALLET = "TFakeRoleModerator000000000001"
ADMIN_WALLET = "TFakeRoleAdmin00000000000000001"
TARGET_WALLET = "TFakeRoleTarget0000000000000001"


def test_member_cannot_perform_moderator_or_admin_actions(client, db_session):
    seed_membership(db_session, MEMBER_WALLET, role=MemberRole.MEMBER)
    seed_membership(db_session, TARGET_WALLET, role=MemberRole.MEMBER)
    token = login(client, MEMBER_WALLET)

    suspend = client.post(f"/admin/memberships/999999/suspend", json={"reason": "test"}, headers=auth_headers(token))
    assert suspend.status_code == 403

    create_invite = client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 24}, headers=auth_headers(token))
    assert create_invite.status_code == 403

    reports = client.get("/moderation/reports", headers=auth_headers(token))
    assert reports.status_code == 403


def test_moderator_cannot_change_blockchain_state(client, db_session, fake_chain):
    """No existe NINGUN endpoint bajo /admin (MODERATOR incluido) que ESCRIBA
    onchain_status — verificado estructuralmente sobre el codigo fuente. Desde
    Phase 2B.1, admin.py SI importa P2POrderDB (para validar, de solo lectura,
    que una orden existe y esta DISPUTED antes de crear un DisputeAssignment) —
    lo que se prueba aqui es que nunca lo ESCRIBE."""
    import inspect

    from backend.routers import admin as admin_router

    source = inspect.getsource(admin_router)
    assert "P2POrderDB(" not in source  # nunca construye/inserta una fila de orden
    assert "onchain_status =" not in source  # nunca asigna el campo (solo lo LEE en un filter())
    assert ".release(" not in source
    assert ".refund(" not in source


def test_moderator_cannot_read_unrelated_settlement_details(client, db_session, fake_chain):
    seed_membership(db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)
    seller = "TFakeRoleSeller00000000000001"
    buyer = "TFakeRoleBuyer000000000000001"
    seed_membership(db_session, seller, role=MemberRole.MEMBER)

    fake_chain.seed_order_created(1, seller, "TOKEN", 100_000000)
    fake_chain.seed_order_claimed(1, buyer, "TFakeRoleArbiter00000000000001")
    fake_chain.seed_paid(1)
    # NO disputed — a proposito
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    seller_token = login(client, seller)
    detail = client.post(
        "/settlement-details", json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": "SYN-1"}}, headers=auth_headers(seller_token)
    ).json()
    client.post(
        "/orders/metadata",
        json={"onchain_order_id": 1, "fiat_amount": "10.00", "fiat_currency": "CUP", "payment_method": "Transferencia CUP", "settlement_detail_id": detail["id"]},
        headers=auth_headers(seller_token),
    )

    moderator_token = login(client, MODERATOR_WALLET)
    r = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(moderator_token))
    assert r.status_code == 403  # ni siquiera MODERATOR ve settlement de una orden no disputada de la que no es parte


def test_admin_cannot_release_or_refund_escrow(client, db_session):
    """No existe ningun endpoint /admin/.../release o /refund — verificado
    estructuralmente sobre las rutas registradas."""
    from backend.routers import admin as admin_router

    paths = [getattr(r, "path", "") for r in admin_router.router.routes]
    assert not any("release" in p.lower() or "refund" in p.lower() for p in paths)


def test_privilege_escalation_attempt_denied(client, db_session):
    """Un MEMBER no puede auto-asignarse ADMIN, ni siquiera intentando pegarle a
    su propia membership por id."""
    user = seed_membership(db_session, MEMBER_WALLET, role=MemberRole.MEMBER)
    token = login(client, MEMBER_WALLET)

    r = client.post(f"/admin/memberships/{user.id}/role", json={"role": "admin"}, headers=auth_headers(token))
    assert r.status_code == 403

    membership = db_session.query(MembershipDB).filter(MembershipDB.user_id == user.id).first()
    assert membership.role == MemberRole.MEMBER


def test_role_changes_generate_audit_events(client, db_session):
    admin_user = seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    target_user = seed_membership(db_session, TARGET_WALLET, role=MemberRole.MEMBER)
    admin_token = login(client, ADMIN_WALLET)

    r = client.post(f"/admin/memberships/{target_user.id}/role", json={"role": "moderator"}, headers=auth_headers(admin_token))
    assert r.status_code == 200

    events = db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.ROLE_CHANGED).all()
    assert len(events) == 1
    assert events[0].actor_user_id == admin_user.id
    assert events[0].target_id == str(target_user.id)


def test_suspension_immediately_blocks_marketplace_access(client, db_session):
    admin_user = seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    target_user = seed_membership(db_session, TARGET_WALLET, role=MemberRole.MEMBER)
    admin_token = login(client, ADMIN_WALLET)
    target_token = login(client, TARGET_WALLET)

    before = client.get("/orders/", headers=auth_headers(target_token))
    assert before.status_code == 200

    suspend = client.post(f"/admin/memberships/{target_user.id}/suspend", json={"reason": "synthetic test reason"}, headers=auth_headers(admin_token))
    assert suspend.status_code == 200

    after = client.get("/orders/", headers=auth_headers(target_token))
    assert after.status_code == 403


def test_moderator_can_suspend_but_only_admin_can_reactivate(client, db_session):
    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    seed_membership(db_session, MODERATOR_WALLET, role=MemberRole.MODERATOR)
    target_user = seed_membership(db_session, TARGET_WALLET, role=MemberRole.MEMBER)
    moderator_token = login(client, MODERATOR_WALLET)
    admin_token = login(client, ADMIN_WALLET)

    suspend = client.post(f"/admin/memberships/{target_user.id}/suspend", json={"reason": "moderator action"}, headers=auth_headers(moderator_token))
    assert suspend.status_code == 200

    reactivate_by_moderator = client.post(f"/admin/memberships/{target_user.id}/reactivate", headers=auth_headers(moderator_token))
    assert reactivate_by_moderator.status_code == 403

    reactivate_by_admin = client.post(f"/admin/memberships/{target_user.id}/reactivate", headers=auth_headers(admin_token))
    assert reactivate_by_admin.status_code == 200
