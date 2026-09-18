"""
Phase 2B.2 #4 — transaction boundary audit (see TRANSACTION_BOUNDARIES.md for
the full BEGIN/writes/COMMIT/rollback breakdown of all five critical
transactions). Invite redemption atomicity is already proven under real
concurrency in test_invite_atomicity.py — not duplicated here.

The former two-commit-point limitation this file used to document (primary
write commits, then the audit event commits separately and can fail on its
own) was FIXED in Phase 2B.3 — see test_atomic_audit_log.py for the full set
of DB-level failure-injection rollback tests proving the fix. This file keeps
the two checks that are still specific to this phase and unrelated to that fix.
"""

from backend.models import MemberRole

from .conftest import auth_headers, login, seed_membership

ADMIN_WALLET = "TFakeTxAdmin00000000000000001"
TARGET_WALLET = "TFakeTxTarget0000000000000001"


def test_dispute_assignment_creation_failure_leaves_no_partial_row(client, db_session, fake_chain):
    """Si la validacion en Python rechaza el request (orden no DISPUTED), no
    se ejecuta NINGUN write — no hay fila de assignment parcial ni evento de
    auditoria huerfano."""
    from backend.models import DisputeAssignmentDB, SecurityEventDB, SecurityEventType

    from .conftest import sync_indexer

    fake_chain.seed_order_created(1, "TFakeTxSeller000000000000001", "TOKEN", 100)
    fake_chain.seed_order_claimed(1, "TFakeTxBuyer0000000000000001", "TFakeTxArbiter00000000000001")
    fake_chain.seed_paid(1)  # PAID, no DISPUTED
    sync_indexer(db_session, fake_chain)
    from backend.models import P2POrderDB

    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    seed_membership(db_session, ADMIN_WALLET, role=MemberRole.ADMIN)
    admin_token = login(client, ADMIN_WALLET)
    moderator = seed_membership(db_session, "TFakeTxModerator0000000000001", role=MemberRole.MODERATOR)

    r = client.post(
        f"/admin/disputes/{order.id}/assignments",
        json={"assigned_user_id": moderator.id, "reason": "premature", "expires_in_hours": 24},
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 400

    assert db_session.query(DisputeAssignmentDB).count() == 0
    assert db_session.query(SecurityEventDB).filter(SecurityEventDB.action == SecurityEventType.DISPUTE_ASSIGNMENT_CREATED).count() == 0


def test_settlement_snapshot_write_is_all_or_nothing_with_metadata(client, db_session, fake_chain):
    """attach_order_metadata es una unica transaccion — si el settlement_detail
    referenciado es invalido, NINGUN campo de metadata se escribe (no queda la
    orden a medio-actualizar con fiat_amount seteado pero sin snapshot)."""
    from .conftest import sync_indexer
    from backend.models import P2POrderDB

    fake_chain.seed_order_created(1, "TFakeTxSeller200000000000001", "TOKEN", 100)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    seed_membership(db_session, "TFakeTxSeller200000000000001", role=MemberRole.MEMBER)
    seller_token = login(client, "TFakeTxSeller200000000000001")

    r = client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": 1,
            "fiat_amount": "10.00",
            "fiat_currency": "CUP",
            "payment_method": "Transferencia CUP",
            "settlement_detail_id": 999999,  # no existe
        },
        headers=auth_headers(seller_token),
    )
    assert r.status_code == 400

    db_session.expire_all()
    order = db_session.query(P2POrderDB).filter(P2POrderDB.id == order.id).first()
    assert order.fiat_amount is None  # nada se escribio, ni siquiera los campos "inocuos"
    assert order.settlement_snapshot_payload is None
