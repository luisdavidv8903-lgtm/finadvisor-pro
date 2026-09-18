"""
Phase 2B.3 — every privileged action's primary mutation and its
SecurityEventDB insertion now live in ONE transaction, ONE commit (see
security/audit.py, and every router touched this phase). These tests prove
it with REAL DB-level failure injection, not a mocked return value: the
broken logger attempts a genuine SecurityEventDB insert with a
`actor_user_id` that violates the real `ForeignKey("users.id")` constraint —
the same PRAGMA foreign_keys=ON enforcement proven in
test_sqlite_foreign_keys.py (Phase 2B.2) is what makes this a real,
engine-level IntegrityError, not a fake exception.

For every action: if the audit insert fails, the primary mutation must not
survive either (they're the same transaction, so this is automatic — these
tests verify it's actually true, not just designed to be true).
"""

import pytest
from sqlalchemy.exc import IntegrityError

from backend.models import (
    DisputeAssignmentDB,
    InviteDB,
    MemberRole,
    MembershipDB,
    MembershipStatus,
    P2POrderDB,
    ReportDB,
    SecurityEventDB,
    SettlementDetailDB,
    UserDB,
)

from .conftest import auth_headers, login, seed_membership, sync_indexer

ADMIN_WALLET = "TFakeAtomicAdmin00000000000001"


def _break_logger(module):
    """Reemplaza `module.log_security_event` por una version que SI intenta
    escribir un SecurityEventDB real, pero con actor_user_id apuntando a un
    usuario inexistente — una violacion REAL de la FK (no una excepcion
    inventada), detectada en el flush gracias al PRAGMA foreign_keys=ON
    global de Phase 2B.2."""

    def _broken(db, *, action, actor_user_id, target_type=None, target_id=None, reason=None):
        event = SecurityEventDB(
            actor_user_id=999999999,  # FK invalida a proposito
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            reason=reason,
        )
        db.add(event)
        db.flush()
        return event

    return _broken


def _recover_session(db_session):
    """Despues de un flush fallido, la sesion queda en estado 'needs rollback'
    — hay que revertirla antes de poder usarla para las queries de verificacion."""
    db_session.rollback()


def test_suspend_membership_rolls_back_if_audit_insert_fails(client, db_session, monkeypatch):
    import backend.routers.admin as admin_router

    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    target = seed_membership(db_session, "TFakeAtomicSuspendTarget000001", role=MemberRole.MEMBER)
    admin_token = login(client, ADMIN_WALLET)

    monkeypatch.setattr(admin_router, "log_security_event", _break_logger(admin_router))

    with pytest.raises(IntegrityError):
        client.post(f"/admin/memberships/{target.id}/suspend", json={"reason": "synthetic"}, headers=auth_headers(admin_token))

    _recover_session(db_session)
    membership = db_session.query(MembershipDB).filter(MembershipDB.user_id == target.id).first()
    assert membership.status == MembershipStatus.ACTIVE  # NUNCA se suspendio


def test_reactivate_membership_rolls_back_if_audit_insert_fails(client, db_session, monkeypatch):
    import backend.routers.admin as admin_router

    admin_user = seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    target = seed_membership(db_session, "TFakeAtomicReactivateTarget00001", role=MemberRole.MEMBER, status=MembershipStatus.SUSPENDED)
    admin_token = login(client, ADMIN_WALLET)

    monkeypatch.setattr(admin_router, "log_security_event", _break_logger(admin_router))

    with pytest.raises(IntegrityError):
        client.post(f"/admin/memberships/{target.id}/reactivate", headers=auth_headers(admin_token))

    _recover_session(db_session)
    membership = db_session.query(MembershipDB).filter(MembershipDB.user_id == target.id).first()
    assert membership.status == MembershipStatus.SUSPENDED  # NUNCA se reactivo


def test_role_change_rolls_back_if_audit_insert_fails(client, db_session, monkeypatch):
    import backend.routers.admin as admin_router

    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    target = seed_membership(db_session, "TFakeAtomicRoleTarget00000000001", role=MemberRole.MEMBER)
    admin_token = login(client, ADMIN_WALLET)

    monkeypatch.setattr(admin_router, "log_security_event", _break_logger(admin_router))

    with pytest.raises(IntegrityError):
        client.post(f"/admin/memberships/{target.id}/role", json={"role": "moderator"}, headers=auth_headers(admin_token))

    _recover_session(db_session)
    membership = db_session.query(MembershipDB).filter(MembershipDB.user_id == target.id).first()
    assert membership.role == MemberRole.MEMBER  # el rol viejo permanece


def test_invite_creation_rolls_back_if_audit_insert_fails(client, db_session, monkeypatch):
    import backend.routers.admin as admin_router

    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    admin_token = login(client, ADMIN_WALLET)

    invites_before = db_session.query(InviteDB).count()
    monkeypatch.setattr(admin_router, "log_security_event", _break_logger(admin_router))

    with pytest.raises(IntegrityError):
        client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 24}, headers=auth_headers(admin_token))

    _recover_session(db_session)
    assert db_session.query(InviteDB).count() == invites_before  # ningun invite quedo a medio crear


def test_invite_revocation_rolls_back_if_audit_insert_fails(client, db_session, monkeypatch):
    import backend.routers.admin as admin_router

    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    admin_token = login(client, ADMIN_WALLET)
    created = client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 24}, headers=auth_headers(admin_token)).json()

    monkeypatch.setattr(admin_router, "log_security_event", _break_logger(admin_router))

    with pytest.raises(IntegrityError):
        client.post(f"/admin/invites/{created['id']}/revoke", headers=auth_headers(admin_token))

    _recover_session(db_session)
    invite = db_session.query(InviteDB).filter(InviteDB.id == created["id"]).first()
    assert invite.revoked_at is None  # sigue sin revocar


def test_dispute_assignment_creation_rolls_back_if_audit_insert_fails(client, db_session, fake_chain, monkeypatch):
    import backend.routers.admin as admin_router

    fake_chain.seed_order_created(1, "TFakeAtomicDaSeller0000000001", "TOKEN", 100)
    fake_chain.seed_order_claimed(1, "TFakeAtomicDaBuyer00000000001", "TFakeAtomicDaArbiter000000001")
    fake_chain.seed_paid(1)
    fake_chain.seed_disputed(1)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    admin_token = login(client, ADMIN_WALLET)
    moderator = seed_membership(db_session, "TFakeAtomicDaModerator000000001", role=MemberRole.MODERATOR)

    monkeypatch.setattr(admin_router, "log_security_event", _break_logger(admin_router))

    with pytest.raises(IntegrityError):
        client.post(
            f"/admin/disputes/{order.id}/assignments",
            json={"assigned_user_id": moderator.id, "reason": "synthetic", "expires_in_hours": 24},
            headers=auth_headers(admin_token),
        )

    _recover_session(db_session)
    assert db_session.query(DisputeAssignmentDB).filter(DisputeAssignmentDB.order_id == order.id).count() == 0


def test_dispute_assignment_revocation_rolls_back_if_audit_insert_fails(client, db_session, fake_chain, monkeypatch):
    import backend.routers.admin as admin_router

    fake_chain.seed_order_created(1, "TFakeAtomicDarSeller000000001", "TOKEN", 100)
    fake_chain.seed_order_claimed(1, "TFakeAtomicDarBuyer0000000001", "TFakeAtomicDarArbiter00000001")
    fake_chain.seed_paid(1)
    fake_chain.seed_disputed(1)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    admin_token = login(client, ADMIN_WALLET)
    moderator = seed_membership(db_session, "TFakeAtomicDarModerator0000001", role=MemberRole.MODERATOR)

    assignment = client.post(
        f"/admin/disputes/{order.id}/assignments",
        json={"assigned_user_id": moderator.id, "reason": "synthetic", "expires_in_hours": 24},
        headers=auth_headers(admin_token),
    ).json()

    monkeypatch.setattr(admin_router, "log_security_event", _break_logger(admin_router))

    with pytest.raises(IntegrityError):
        client.post(f"/admin/disputes/assignments/{assignment['id']}/revoke", headers=auth_headers(admin_token))

    _recover_session(db_session)
    persisted = db_session.query(DisputeAssignmentDB).filter(DisputeAssignmentDB.id == assignment["id"]).first()
    assert persisted.revoked_at is None  # sigue activa


def test_settlement_creation_rolls_back_if_audit_insert_fails(client, db_session, monkeypatch):
    import backend.routers.settlement as settlement_router

    member_token = login(client, "TFakeAtomicSettleOwner0000001")
    seed_membership(db_session, "TFakeAtomicSettleOwner0000001", role=MemberRole.MEMBER)
    member_token = login(client, "TFakeAtomicSettleOwner0000001")

    details_before = db_session.query(SettlementDetailDB).count()
    monkeypatch.setattr(settlement_router, "log_security_event", _break_logger(settlement_router))

    with pytest.raises(IntegrityError):
        client.post(
            "/settlement-details",
            json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": "SYN-ATOMIC-ROLLBACK"}},
            headers=auth_headers(member_token),
        )

    _recover_session(db_session)
    # ningun registro sensible sobrevivio — ni siquiera parcialmente
    assert db_session.query(SettlementDetailDB).count() == details_before


def test_settlement_revocation_rolls_back_if_audit_insert_fails(client, db_session, monkeypatch):
    import backend.routers.settlement as settlement_router

    member_token = login(client, "TFakeAtomicSettleRevoke0000001")
    seed_membership(db_session, "TFakeAtomicSettleRevoke0000001", role=MemberRole.MEMBER)
    member_token = login(client, "TFakeAtomicSettleRevoke0000001")

    detail = client.post(
        "/settlement-details",
        json={"payment_method": "cup_transfer", "currency": "CUP", "payload": {"account_number": "SYN-ATOMIC-2"}},
        headers=auth_headers(member_token),
    ).json()

    monkeypatch.setattr(settlement_router, "log_security_event", _break_logger(settlement_router))

    with pytest.raises(IntegrityError):
        client.post(f"/settlement-details/{detail['id']}/revoke", headers=auth_headers(member_token))

    _recover_session(db_session)
    persisted = db_session.query(SettlementDetailDB).filter(SettlementDetailDB.id == detail["id"]).first()
    assert persisted.active is True  # sigue activo, nunca se revoco


def test_report_review_rolls_back_if_audit_insert_fails(client, db_session, monkeypatch):
    import backend.routers.reports as reports_router

    reporter_token = login(client, "TFakeAtomicReporter00000000001")
    seed_membership(db_session, "TFakeAtomicReporter00000000001", role=MemberRole.MEMBER)
    reporter_token = login(client, "TFakeAtomicReporter00000000001")
    report = client.post("/reports", json={"reason": "synthetic report"}, headers=auth_headers(reporter_token)).json()

    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    admin_token = login(client, ADMIN_WALLET)

    monkeypatch.setattr(reports_router, "log_security_event", _break_logger(reports_router))

    with pytest.raises(IntegrityError):
        client.post(f"/moderation/reports/{report['id']}/review", json={"status": "dismissed"}, headers=auth_headers(admin_token))

    _recover_session(db_session)
    persisted = db_session.query(ReportDB).filter(ReportDB.id == report["id"]).first()
    assert persisted.status == "open"  # nunca cambio


def test_invite_redemption_rolls_back_if_audit_insert_fails(db_session, fake_chain):
    """La logica vive en services/invites.py — se prueba a nivel de servicio,
    como el resto de test_invite_atomicity.py, para controlar exactamente el
    punto de fallo."""
    from datetime import datetime, timedelta, timezone

    import backend.services.invites as invites_service
    from backend.services.invites import InviteRedemptionError, hash_invite_code, redeem_invite_for_user

    admin = UserDB(wallet_address="TFakeAtomicInviteAdmin00000001")
    db_session.add(admin)
    db_session.commit()

    code = "synthetic-atomic-rollback-code"
    invite = InviteDB(
        code_hash=hash_invite_code(code),
        created_by_user_id=admin.id,
        max_uses=5,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(invite)
    db_session.commit()
    used_count_before = invite.used_count

    redeemer = UserDB(wallet_address="TFakeAtomicInviteRedeemer000001")
    db_session.add(redeemer)
    db_session.commit()

    original_log = invites_service.log_security_event
    broken = _break_logger(invites_service)

    def _patched(db, **kwargs):
        return broken(db, **kwargs)

    invites_service.log_security_event = _patched
    try:
        with pytest.raises((InviteRedemptionError, IntegrityError)):
            redeem_invite_for_user(db_session, code, redeemer)
    finally:
        invites_service.log_security_event = original_log

    _recover_session(db_session)
    assert db_session.query(MembershipDB).filter(MembershipDB.user_id == redeemer.id).first() is None
    refreshed_invite = db_session.query(InviteDB).filter(InviteDB.id == invite.id).first()
    assert refreshed_invite.used_count == used_count_before  # el cupo nunca se consumio


def test_bootstrap_admin_rolls_back_if_audit_insert_fails(tmp_path, monkeypatch):
    """ADMIN bootstrap — usa una DB real migrada con Alembic (mismo patron que
    test_bootstrap_admin.py), no el fixture rapido, porque bootstrap_admin()
    exige la tabla alembic_version como precondicion."""
    import pathlib

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import backend.bootstrap_admin as bootstrap_module
    from backend.bootstrap_admin import BootstrapError, bootstrap_admin

    from .fake_chain_adapter import FakeChainAdapter

    backend_dir = pathlib.Path(bootstrap_module.__file__).resolve().parent
    db_path = tmp_path / "bootstrap_rollback_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = Session()

    monkeypatch.setattr(bootstrap_module, "log_security_event", _break_logger(bootstrap_module))

    with pytest.raises((BootstrapError, IntegrityError)):
        bootstrap_admin(db, FakeChainAdapter(), "TFakeAtomicBootstrap0000000001")

    db.rollback()
    assert db.query(UserDB).filter(UserDB.wallet_address == "TFakeAtomicBootstrap0000000001").first() is None
    assert db.query(MembershipDB).filter(MembershipDB.role == MemberRole.ADMIN).count() == 0
    db.close()
    engine.dispose()
