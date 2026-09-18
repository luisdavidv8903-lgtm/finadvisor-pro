"""Phase 2B.5 — application-wide UTC-aware time convention.

All DateTime columns are DateTime(timezone=True). PostgreSQL returns aware
datetimes for these natively (see test_postgres_validation.py for the
POSTGRES_VERIFIED round-trip); SQLite always returns naive datetimes
regardless of the column declaration, so `timeutils.ensure_utc()` is the
normalization boundary. These tests run against SQLite (via `db_session`)
and use deterministic timestamps (monkeypatching `timeutils.utcnow`) rather
than sleeping or relying on wall-clock timing, so the boundary is exact.

Cross-dialect parity (same expiration decision on SQLite and PostgreSQL) is
proven by test_postgres_validation.py's mirror tests, which call the exact
same service-layer functions used here against a real Postgres session.
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend import timeutils
from backend.auth import generate_nonce, verify_and_consume_nonce
from backend.config import settings
from backend.models import (
    AuthNonceDB,
    DisputeAssignmentDB,
    InviteDB,
    MemberRole,
    MembershipStatus,
    OrderStatus,
    P2POrderDB,
    UserDB,
)
from backend.services.authorization import has_active_dispute_assignment
from backend.services.invites import InviteRedemptionError, hash_invite_code, redeem_invite_for_user
from backend.services.reputation import get_reputation

FIXED_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _freeze(monkeypatch, at: datetime) -> None:
    monkeypatch.setattr(timeutils, "utcnow", lambda: at)


def _make_user(db_session, wallet: str) -> UserDB:
    user = UserDB(wallet_address=wallet)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# timeutils unit behavior
# ---------------------------------------------------------------------------


def test_utcnow_returns_aware_utc():
    now = timeutils.utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_ensure_utc_none_is_none():
    assert timeutils.ensure_utc(None) is None


def test_ensure_utc_normalizes_naive_as_utc():
    naive = datetime(2026, 3, 1, 8, 30, 0)
    aware = timeutils.ensure_utc(naive)
    assert aware.tzinfo is timezone.utc
    assert aware == datetime(2026, 3, 1, 8, 30, 0, tzinfo=timezone.utc)


def test_ensure_utc_preserves_already_aware_instant():
    other_tz = timezone(timedelta(hours=-5))
    aware = datetime(2026, 3, 1, 3, 30, 0, tzinfo=other_tz)
    result = timeutils.ensure_utc(aware)
    assert result.tzinfo is not None
    assert result == aware  # misma instancia de tiempo, sin importar el offset


# ---------------------------------------------------------------------------
# Nonce expiration at the exact UTC boundary (deterministic)
# ---------------------------------------------------------------------------


def test_nonce_valid_exactly_at_expiry_boundary_is_accepted(db_session, monkeypatch):
    # calculado en Python (no releido de SQLite, que devuelve naive) para
    # mantener el "now" congelado siempre aware, igual que en produccion.
    expires_at = FIXED_NOW + timedelta(seconds=settings.NONCE_EXPIRE_SECONDS)
    _freeze(monkeypatch, FIXED_NOW)
    nonce = generate_nonce(db_session, "0xNonceBoundary1")

    # el momento exacto de expiracion es INCLUSIVO (expires_at >= now en auth.py)
    _freeze(monkeypatch, expires_at)
    assert verify_and_consume_nonce(db_session, "0xNonceBoundary1", nonce) is True


def test_nonce_one_microsecond_past_expiry_is_rejected(db_session, monkeypatch):
    expires_at = FIXED_NOW + timedelta(seconds=settings.NONCE_EXPIRE_SECONDS)
    _freeze(monkeypatch, FIXED_NOW)
    nonce = generate_nonce(db_session, "0xNonceBoundary2")

    _freeze(monkeypatch, expires_at + timedelta(microseconds=1))
    assert verify_and_consume_nonce(db_session, "0xNonceBoundary2", nonce) is False


def test_nonce_comparison_never_raises_naive_aware_typeerror(db_session, monkeypatch):
    # regresion: verify_and_consume_nonce compara AuthNonceDB.expires_at (leido
    # de SQLite, naive) contra `now` (aware) en la evaluacion Python interna de
    # synchronize_session=False — esto NO debe lanzar TypeError.
    _freeze(monkeypatch, FIXED_NOW)
    nonce = generate_nonce(db_session, "0xNonceNoTypeError")
    _freeze(monkeypatch, FIXED_NOW + timedelta(seconds=30))
    verify_and_consume_nonce(db_session, "0xNonceNoTypeError", nonce)  # no debe lanzar


# ---------------------------------------------------------------------------
# Invite expiration (deterministic)
# ---------------------------------------------------------------------------


def _make_invite(db_session, creator: UserDB, code: str, expires_at: datetime) -> InviteDB:
    invite = InviteDB(
        code_hash=hash_invite_code(code),
        created_by_user_id=creator.id,
        max_uses=1,
        used_count=0,
        expires_at=expires_at,
    )
    db_session.add(invite)
    db_session.commit()
    return invite


def test_invite_redeemable_exactly_at_expiry_boundary(db_session, monkeypatch):
    admin = _make_user(db_session, "0xInviteAdmin1")
    redeemer = _make_user(db_session, "0xInviteRedeemer1")
    _make_invite(db_session, admin, "code-boundary-ok", expires_at=FIXED_NOW)

    _freeze(monkeypatch, FIXED_NOW)
    membership = redeem_invite_for_user(db_session, "code-boundary-ok", redeemer)
    assert membership.user_id == redeemer.id


def test_invite_one_second_past_expiry_is_denied(db_session, monkeypatch):
    admin = _make_user(db_session, "0xInviteAdmin2")
    redeemer = _make_user(db_session, "0xInviteRedeemer2")
    _make_invite(db_session, admin, "code-boundary-expired", expires_at=FIXED_NOW)

    _freeze(monkeypatch, FIXED_NOW + timedelta(seconds=1))
    with pytest.raises(InviteRedemptionError):
        redeem_invite_for_user(db_session, "code-boundary-expired", redeemer)


# ---------------------------------------------------------------------------
# Dispute assignment expiration (deterministic)
# ---------------------------------------------------------------------------


def _make_order(db_session, onchain_order_id: int) -> P2POrderDB:
    order = P2POrderDB(
        onchain_order_id=onchain_order_id,
        token_address="TTokenAddress",
        seller_wallet="0xSeller",
        buyer_wallet="0xBuyer",
        crypto_amount=100,
        onchain_status=OrderStatus.DISPUTED,
        was_disputed=True,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def test_dispute_assignment_active_exactly_at_expiry_boundary(db_session, monkeypatch):
    order = _make_order(db_session, onchain_order_id=9001)
    admin = _make_user(db_session, "0xAssignAdmin1")
    moderator = _make_user(db_session, "0xAssignMod1")
    assignment = DisputeAssignmentDB(
        order_id=order.id,
        assigned_user_id=moderator.id,
        assigned_by_user_id=admin.id,
        reason="synthetic",
        expires_at=FIXED_NOW,
    )
    db_session.add(assignment)
    db_session.commit()

    _freeze(monkeypatch, FIXED_NOW)
    assert has_active_dispute_assignment(db_session, order, moderator) is True


def test_dispute_assignment_expired_is_inactive(db_session, monkeypatch):
    order = _make_order(db_session, onchain_order_id=9002)
    admin = _make_user(db_session, "0xAssignAdmin2")
    moderator = _make_user(db_session, "0xAssignMod2")
    assignment = DisputeAssignmentDB(
        order_id=order.id,
        assigned_user_id=moderator.id,
        assigned_by_user_id=admin.id,
        reason="synthetic",
        expires_at=FIXED_NOW,
    )
    db_session.add(assignment)
    db_session.commit()

    _freeze(monkeypatch, FIXED_NOW + timedelta(seconds=1))
    assert has_active_dispute_assignment(db_session, order, moderator) is False


# ---------------------------------------------------------------------------
# Suspension / reactivation timestamp round-trip
# ---------------------------------------------------------------------------


def test_suspension_and_reactivation_timestamps(db_session, monkeypatch):
    from backend.models import MembershipDB

    user = _make_user(db_session, "0xSuspendMe")
    membership = MembershipDB(user_id=user.id, role=MemberRole.MEMBER, status=MembershipStatus.ACTIVE)
    db_session.add(membership)
    db_session.commit()

    _freeze(monkeypatch, FIXED_NOW)
    membership.status = MembershipStatus.SUSPENDED
    membership.suspended_at = timeutils.utcnow()
    db_session.commit()
    db_session.refresh(membership)
    assert timeutils.ensure_utc(membership.suspended_at) == FIXED_NOW

    membership.status = MembershipStatus.ACTIVE
    membership.suspended_at = None
    db_session.commit()
    db_session.refresh(membership)
    assert membership.suspended_at is None


# ---------------------------------------------------------------------------
# No naive/aware TypeError when reading SQLite-stored timestamps in Python
# ---------------------------------------------------------------------------


def test_reputation_account_age_no_naive_aware_typeerror(db_session):
    user = _make_user(db_session, "0xReputationAge")
    # user.created_at fue escrito por el default de la columna (aware) pero
    # SQLite lo devuelve naive al releerlo en esta misma sesion tras el flush
    # -- get_reputation debe normalizar con ensure_utc() antes de restar.
    summary = get_reputation(db_session, user)
    assert summary.account_age_days >= 0
