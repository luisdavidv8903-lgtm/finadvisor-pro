"""
Phase 2B.2 #1 — PRAGMA foreign_keys=ON is enabled globally (database.py, an
Engine-class-level `connect` event, not a per-test manual PRAGMA) for every
SQLite connection in this process, including the ones `db_session`/`client`
fixtures create. These tests prove the DATABASE rejects the insert via raw SQL
— no ORM relationship/object graph involved, so this cannot be satisfied by
SQLAlchemy-side validation alone.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .conftest import seed_membership


def test_membership_referencing_nonexistent_user_rejected(db_session):
    with pytest.raises(IntegrityError):
        db_session.execute(
            text("INSERT INTO memberships (user_id, role, status) VALUES (:uid, 'MEMBER', 'ACTIVE')"),
            {"uid": 999999},
        )
        db_session.commit()
    db_session.rollback()


def test_settlement_detail_referencing_nonexistent_owner_rejected(db_session):
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO settlement_details (owner_user_id, payment_method, encrypted_payload, active) "
                "VALUES (:uid, 'cup_transfer', :payload, 1)"
            ),
            {"uid": 999999, "payload": b"synthetic-ciphertext"},
        )
        db_session.commit()
    db_session.rollback()


def test_dispute_evidence_referencing_nonexistent_order_rejected(db_session):
    user = seed_membership(db_session, "TFakeFkEvidenceUser000000000001")
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO dispute_evidence (order_id, submitted_by_user_id, evidence_type) "
                "VALUES (:oid, :uid, 'note')"
            ),
            {"oid": 999999, "uid": user.id},
        )
        db_session.commit()
    db_session.rollback()


def test_dispute_assignment_referencing_nonexistent_order_rejected(db_session):
    user = seed_membership(db_session, "TFakeFkAssignUser0000000000001")
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO dispute_assignments (order_id, assigned_user_id, assigned_by_user_id, reason, expires_at) "
                "VALUES (:oid, :uid, :uid, 'synthetic', '2030-01-01 00:00:00')"
            ),
            {"oid": 999999, "uid": user.id},
        )
        db_session.commit()
    db_session.rollback()


def test_dispute_assignment_referencing_nonexistent_user_rejected(db_session, fake_chain):
    from .conftest import sync_indexer

    fake_chain.seed_order_created(1, "TFakeFkOrderSeller000000000001", "TOKEN", 100)
    sync_indexer(db_session, fake_chain)
    from backend.models import P2POrderDB

    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()

    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO dispute_assignments (order_id, assigned_user_id, assigned_by_user_id, reason, expires_at) "
                "VALUES (:oid, :uid, :uid, 'synthetic', '2030-01-01 00:00:00')"
            ),
            {"oid": order.id, "uid": 999999},
        )
        db_session.commit()
    db_session.rollback()


def test_pragma_is_actually_on_for_this_connection(db_session):
    """Confirmacion directa (no solo inferida del comportamiento de rechazo de
    arriba) de que la conexion realmente tiene el PRAGMA activo."""
    result = db_session.execute(text("PRAGMA foreign_keys")).scalar()
    assert result == 1


def test_valid_foreign_keys_still_insert_successfully(db_session):
    """Sanity check: el PRAGMA rechaza referencias invalidas pero NO rompe
    inserts legitimos con FKs validas."""
    user = seed_membership(db_session, "TFakeFkValidUser00000000000001")
    db_session.execute(
        text(
            "INSERT INTO settlement_details (owner_user_id, payment_method, encrypted_payload, active) "
            "VALUES (:uid, 'cup_transfer', :payload, 1)"
        ),
        {"uid": user.id, "payload": b"synthetic-ciphertext"},
    )
    db_session.commit()  # no debe lanzar
