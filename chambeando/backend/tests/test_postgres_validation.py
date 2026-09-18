"""
Phase 2B.4 — critical persistence/concurrency/security behavior verified
against a REAL local PostgreSQL 17.11 instance, not SQLite. Skips cleanly
(whole module) if POSTGRES_TEST_DATABASE_URL is not set in the environment,
so the normal SQLite suite (test_*.py elsewhere in this directory) never
requires Postgres to be running. Labels used throughout this file's docstrings
and the Phase 2B.4 report: POSTGRES_VERIFIED vs SQLITE_VERIFIED — this file is
exclusively the former.

Isolation strategy: migrate the schema ONCE per test session (real
`alembic upgrade head` against the dedicated `chambeando_test` database), then
TRUNCATE ... RESTART IDENTITY CASCADE before every test function — fast, and
still against the real migrated schema (not create_all()).
"""

import os
import pathlib
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

POSTGRES_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="POSTGRES_TEST_DATABASE_URL not set — PostgreSQL validation skipped (see Phase 2B.4 report)"
)

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def pg_engine():
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = POSTGRES_URL  # leido por alembic/env.py en cada invocacion
    command.upgrade(cfg, "head")

    engine = create_engine(POSTGRES_URL)
    yield engine
    engine.dispose()
    # restaurar el entorno global: no dejar DATABASE_URL apuntando a Postgres
    # para el resto de la sesion de tests si este modulo corrio junto a otros.
    if previous_database_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous_database_url


@pytest.fixture()
def pg_session(pg_engine):
    from backend.database import Base

    Session = sessionmaker(bind=pg_engine, autocommit=False, autoflush=False)
    session = Session()
    # TRUNCATE de todas las tablas de la app (nunca alembic_version) para aislar
    # cada test contra el esquema YA migrado, sin re-correr Alembic por test.
    table_names = [t.name for t in Base.metadata.sorted_tables]
    session.execute(text(f'TRUNCATE TABLE {", ".join(table_names)} RESTART IDENTITY CASCADE'))
    session.commit()
    yield session
    session.rollback()
    session.close()


def _break_logger(module):
    from backend.models import SecurityEventDB

    def _broken(db, *, action, actor_user_id, target_type=None, target_id=None, reason=None):
        event = SecurityEventDB(
            actor_user_id=999999999,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            reason=reason,
        )
        db.add(event)
        db.flush()
        return event

    return _broken


# ---------------------------------------------------------------------------
# Invite concurrency — real Postgres connections, real OS threads
# ---------------------------------------------------------------------------


def test_invite_max_uses_concurrency_two_simultaneous_attempts_for_final_slot(pg_engine):
    """POSTGRES_VERIFIED: dos hilos, dos conexiones Postgres independientes,
    compitiendo por el ultimo cupo de un invite max_uses=1.

    Esta prueba necesita el `pg_engine` (module-scoped, sin TRUNCATE por test)
    en vez de `pg_session`, porque abre sus propias conexiones concurrentes
    desde threads — por eso hace su PROPIO TRUNCATE al inicio: sin esto, dos
    ejecuciones consecutivas (o correr este archivo junto al resto de la
    suite, donde otro test ya insirtio filas en users/memberships) chocan con
    datos de una corrida anterior (encontrado corriendo la suite combinada:
    ver informe de Phase 2B.4)."""
    from backend.database import Base
    from backend.models import InviteDB, MembershipDB, UserDB
    from backend.services.invites import InviteRedemptionError, hash_invite_code, redeem_invite_for_user

    Session = sessionmaker(bind=pg_engine, autocommit=False, autoflush=False)
    setup = Session()
    table_names = [t.name for t in Base.metadata.sorted_tables]
    setup.execute(text(f'TRUNCATE TABLE {", ".join(table_names)} RESTART IDENTITY CASCADE'))
    setup.commit()

    admin = UserDB(wallet_address="TFakePgConcAdmin000000000000001")
    setup.add(admin)
    setup.commit()

    code = "synthetic-pg-concurrency-code"
    invite = InviteDB(
        code_hash=hash_invite_code(code),
        created_by_user_id=admin.id,
        max_uses=1,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    setup.add(invite)
    setup.commit()

    user_a = UserDB(wallet_address="TFakePgConcUserA00000000000001")
    user_b = UserDB(wallet_address="TFakePgConcUserB00000000000001")
    setup.add_all([user_a, user_b])
    setup.commit()
    user_a_id, user_b_id, invite_id = user_a.id, user_b.id, invite.id
    setup.close()

    results = {}
    barrier = threading.Barrier(2)

    def attempt(key, user_id):
        session = Session()
        try:
            user = session.query(UserDB).filter(UserDB.id == user_id).first()
            barrier.wait()
            membership = redeem_invite_for_user(session, code, user)
            results[key] = ("success", membership.id)
        except InviteRedemptionError as exc:
            results[key] = ("denied", exc.reason)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=("a", user_a_id)), threading.Thread(target=attempt, args=("b", user_b_id))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outcomes = [v[0] for v in results.values()]
    assert outcomes.count("success") == 1
    assert outcomes.count("denied") == 1

    verify = Session()
    try:
        final_invite = verify.query(InviteDB).filter(InviteDB.id == invite_id).first()
        assert final_invite.used_count == 1
        assert verify.query(MembershipDB).count() == 1
    finally:
        # try/finally a proposito: si una assert de arriba falla, esta sesion
        # NO debe quedar "idle in transaction" bloqueando el TRUNCATE del
        # siguiente test con un lock sobre memberships/invites (encontrado
        # corriendo la suite combinada contra Postgres real — ver informe).
        verify.close()


# ---------------------------------------------------------------------------
# Constraint rejection — real DB, not SQLAlchemy-side validation
# ---------------------------------------------------------------------------


def test_duplicate_membership_rejected(pg_session):
    """POSTGRES_VERIFIED: uq_membership_user."""
    from backend.models import MembershipDB, UserDB

    user = UserDB(wallet_address="TFakePgDupMember0000000000001")
    pg_session.add(user)
    pg_session.commit()

    pg_session.add(MembershipDB(user_id=user.id))
    pg_session.commit()

    pg_session.add(MembershipDB(user_id=user.id))
    with pytest.raises(IntegrityError):
        pg_session.commit()
    pg_session.rollback()


def test_duplicate_onchain_order_id_rejected(pg_session):
    """POSTGRES_VERIFIED: uq_order_onchain_id."""
    from backend.models import OrderStatus, P2POrderDB

    pg_session.add(
        P2POrderDB(onchain_order_id=1, token_address="T", seller_wallet="TSeller1", crypto_amount=100, onchain_status=OrderStatus.OPEN)
    )
    pg_session.commit()

    pg_session.add(
        P2POrderDB(onchain_order_id=1, token_address="T", seller_wallet="TSeller2", crypto_amount=200, onchain_status=OrderStatus.OPEN)
    )
    with pytest.raises(IntegrityError):
        pg_session.commit()
    pg_session.rollback()


def test_invalid_fk_rejected(pg_session):
    """POSTGRES_VERIFIED: FK enforcement is unconditional on Postgres (unlike
    SQLite, which needs the PRAGMA — see ENFORCEMENT_LEVELS.md)."""
    with pytest.raises(IntegrityError):
        pg_session.execute(
            text("INSERT INTO memberships (user_id, role, status) VALUES (:uid, 'MEMBER', 'ACTIVE')"), {"uid": 999999999}
        )
        pg_session.commit()
    pg_session.rollback()


def test_invalid_enum_rejected(pg_session):
    """POSTGRES_VERIFIED: native ENUM type rejects an out-of-set value —
    different mechanism than SQLite's CHECK constraint, same net guarantee."""
    from backend.models import UserDB

    user = UserDB(wallet_address="TFakePgBadEnum00000000000001")
    pg_session.add(user)
    pg_session.commit()

    with pytest.raises(Exception):  # psycopg2 raises a DataError wrapped by SQLAlchemy, not IntegrityError, for bad enum input
        pg_session.execute(
            text("INSERT INTO memberships (user_id, role, status) VALUES (:uid, 'NOT_A_REAL_ROLE', 'ACTIVE')"), {"uid": user.id}
        )
        pg_session.commit()
    pg_session.rollback()


# ---------------------------------------------------------------------------
# Settlement encryption + snapshot
# ---------------------------------------------------------------------------


def test_settlement_encryption_round_trip(pg_session, monkeypatch):
    """POSTGRES_VERIFIED: BYTEA round-trips the Fernet ciphertext exactly."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    from backend import config as config_module

    monkeypatch.setattr(config_module.settings, "SETTLEMENT_ENCRYPTION_KEY", key)
    from backend.security import crypto as crypto_module

    crypto_module.reset_fernet_cache_for_tests()

    from backend.models import SettlementDetailDB, UserDB
    from backend.security.crypto import decrypt_settlement_payload, encrypt_settlement_payload

    user = UserDB(wallet_address="TFakePgSettleEnc00000000001")
    pg_session.add(user)
    pg_session.commit()

    payload = {"account_number": "SYN-PG-ENCRYPTION-TEST"}
    detail = SettlementDetailDB(owner_user_id=user.id, payment_method="cup_transfer", encrypted_payload=encrypt_settlement_payload(payload))
    pg_session.add(detail)
    pg_session.commit()

    pg_session.expire_all()
    reloaded = pg_session.query(SettlementDetailDB).filter(SettlementDetailDB.id == detail.id).first()
    assert bytes(reloaded.encrypted_payload) != str(payload).encode()  # no esta en claro
    assert decrypt_settlement_payload(reloaded.encrypted_payload) == payload
    crypto_module.reset_fernet_cache_for_tests()


def test_settlement_snapshot_persistence(pg_session, monkeypatch):
    """POSTGRES_VERIFIED: el snapshot congelado (Phase 2B.2) persiste
    correctamente como BYTEA en p2p_orders, independiente del registro maestro."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    from backend import config as config_module

    monkeypatch.setattr(config_module.settings, "SETTLEMENT_ENCRYPTION_KEY", key)
    from backend.security import crypto as crypto_module

    crypto_module.reset_fernet_cache_for_tests()

    from backend.models import OrderStatus, P2POrderDB, SettlementDetailDB, UserDB
    from backend.security.crypto import encrypt_settlement_payload

    user = UserDB(wallet_address="TFakePgSnapshot0000000000001")
    pg_session.add(user)
    pg_session.commit()

    detail = SettlementDetailDB(
        owner_user_id=user.id, payment_method="cup_transfer", encrypted_payload=encrypt_settlement_payload({"account_number": "SYN-SNAP"})
    )
    pg_session.add(detail)
    pg_session.commit()

    order = P2POrderDB(
        onchain_order_id=1,
        token_address="T",
        seller_wallet="TFakePgSnapshot0000000000001",
        crypto_amount=100,
        onchain_status=OrderStatus.CLAIMED,
        settlement_detail_id=detail.id,
        settlement_snapshot_payload=detail.encrypted_payload,
        settlement_snapshot_payment_method=detail.payment_method,
    )
    pg_session.add(order)
    pg_session.commit()

    # revocar el maestro — el snapshot en p2p_orders no debe cambiar
    detail.active = False
    pg_session.commit()

    pg_session.expire_all()
    reloaded_order = pg_session.query(P2POrderDB).filter(P2POrderDB.id == order.id).first()
    assert bytes(reloaded_order.settlement_snapshot_payload) == bytes(detail.encrypted_payload)
    crypto_module.reset_fernet_cache_for_tests()


# ---------------------------------------------------------------------------
# Dispute assignment
# ---------------------------------------------------------------------------


def test_dispute_assignment_creation_and_revocation(pg_session):
    """POSTGRES_VERIFIED."""
    from backend.models import DisputeAssignmentDB, MemberRole, MembershipDB, OrderStatus, P2POrderDB, UserDB

    admin = UserDB(wallet_address="TFakePgDaAdmin00000000000001")
    moderator = UserDB(wallet_address="TFakePgDaModerator000000001")
    pg_session.add_all([admin, moderator])
    pg_session.commit()
    pg_session.add(MembershipDB(user_id=moderator.id, role=MemberRole.MODERATOR))
    pg_session.commit()

    order = P2POrderDB(
        onchain_order_id=1, token_address="T", seller_wallet="S", crypto_amount=100, onchain_status=OrderStatus.DISPUTED
    )
    pg_session.add(order)
    pg_session.commit()

    assignment = DisputeAssignmentDB(
        order_id=order.id,
        assigned_user_id=moderator.id,
        assigned_by_user_id=admin.id,
        reason="synthetic",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    pg_session.add(assignment)
    pg_session.commit()
    assert assignment.revoked_at is None

    assignment.revoked_at = datetime.now(timezone.utc)
    pg_session.commit()
    pg_session.expire_all()
    reloaded = pg_session.query(DisputeAssignmentDB).filter(DisputeAssignmentDB.id == assignment.id).first()
    assert reloaded.revoked_at is not None


def test_expired_assignment_denial(pg_session):
    """POSTGRES_VERIFIED: la misma logica de services/authorization.py contra
    filas reales de Postgres — una asignacion con expires_at en el pasado no
    otorga acceso."""
    from backend.models import DisputeAssignmentDB, MemberRole, MembershipDB, OrderStatus, P2POrderDB, UserDB
    from backend.services.authorization import has_active_dispute_assignment

    moderator = UserDB(wallet_address="TFakePgExpiredMod0000000001")
    pg_session.add(moderator)
    pg_session.commit()
    pg_session.add(MembershipDB(user_id=moderator.id, role=MemberRole.MODERATOR))
    pg_session.commit()

    order = P2POrderDB(onchain_order_id=1, token_address="T", seller_wallet="S", crypto_amount=100, onchain_status=OrderStatus.DISPUTED)
    pg_session.add(order)
    pg_session.commit()

    assignment = DisputeAssignmentDB(
        order_id=order.id,
        assigned_user_id=moderator.id,
        assigned_by_user_id=moderator.id,
        reason="synthetic",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),  # ya expirada
    )
    pg_session.add(assignment)
    pg_session.commit()

    assert has_active_dispute_assignment(pg_session, order, moderator) is False


# ---------------------------------------------------------------------------
# Atomic privileged mutation + audit rollback (real FK-violation injection)
# ---------------------------------------------------------------------------


def test_role_change_rollback_with_real_fk_violation(pg_session):
    """POSTGRES_VERIFIED: mismo mecanismo de inyeccion de fallo real (FK
    invalida) que test_atomic_audit_log.py, contra Postgres real."""
    from backend.models import MemberRole, MembershipDB, SecurityEventType, UserDB
    from backend.security import audit as audit_module

    user = UserDB(wallet_address="TFakePgRoleRollback00000001")
    pg_session.add(user)
    pg_session.commit()
    membership = MembershipDB(user_id=user.id, role=MemberRole.MEMBER)
    pg_session.add(membership)
    pg_session.commit()

    broken = _break_logger(audit_module)
    membership.role = MemberRole.ADMIN
    try:
        broken(pg_session, action=SecurityEventType.ROLE_CHANGED, actor_user_id=user.id, target_type="user", target_id=user.id)
        pg_session.commit()
        assert False, "expected IntegrityError"
    except IntegrityError:
        pg_session.rollback()

    pg_session.expire_all()
    reloaded = pg_session.query(MembershipDB).filter(MembershipDB.user_id == user.id).first()
    assert reloaded.role == MemberRole.MEMBER  # nunca cambio


def test_settlement_creation_rollback_with_real_fk_violation(pg_session):
    """POSTGRES_VERIFIED."""
    from backend.models import SettlementDetailDB, UserDB
    from backend.security import audit as audit_module
    from backend.models import SecurityEventType

    user = UserDB(wallet_address="TFakePgSettleRollback000001")
    pg_session.add(user)
    pg_session.commit()

    before = pg_session.query(SettlementDetailDB).count()
    broken = _break_logger(audit_module)
    detail = SettlementDetailDB(owner_user_id=user.id, payment_method="cup_transfer", encrypted_payload=b"synthetic-ciphertext")
    pg_session.add(detail)
    pg_session.flush()
    try:
        broken(pg_session, action=SecurityEventType.SETTLEMENT_DETAIL_CREATED, actor_user_id=user.id, target_type="settlement_detail", target_id=detail.id)
        pg_session.commit()
        assert False, "expected IntegrityError"
    except IntegrityError:
        pg_session.rollback()

    assert pg_session.query(SettlementDetailDB).count() == before  # ningun registro sensible sobrevivio


def test_invite_redemption_rollback_with_real_fk_violation(pg_session):
    """POSTGRES_VERIFIED."""
    from backend.models import InviteDB, MembershipDB, UserDB
    from backend.services import invites as invites_service
    from backend.services.invites import InviteRedemptionError, hash_invite_code, redeem_invite_for_user

    admin = UserDB(wallet_address="TFakePgInviteRbAdmin0000001")
    pg_session.add(admin)
    pg_session.commit()

    code = "synthetic-pg-rollback-code"
    invite = InviteDB(code_hash=hash_invite_code(code), created_by_user_id=admin.id, max_uses=5, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    pg_session.add(invite)
    pg_session.commit()
    used_before = invite.used_count

    redeemer = UserDB(wallet_address="TFakePgInviteRbUser00000001")
    pg_session.add(redeemer)
    pg_session.commit()

    original = invites_service.log_security_event
    invites_service.log_security_event = _break_logger(invites_service)
    try:
        with pytest.raises((InviteRedemptionError, IntegrityError)):
            redeem_invite_for_user(pg_session, code, redeemer)
    finally:
        invites_service.log_security_event = original

    pg_session.rollback()
    assert pg_session.query(MembershipDB).filter(MembershipDB.user_id == redeemer.id).first() is None
    reloaded_invite = pg_session.query(InviteDB).filter(InviteDB.id == invite.id).first()
    assert reloaded_invite.used_count == used_before


# ---------------------------------------------------------------------------
# Bootstrap admin
# ---------------------------------------------------------------------------


def test_bootstrap_first_admin_succeeds(pg_session):
    """POSTGRES_VERIFIED."""
    from backend.bootstrap_admin import bootstrap_admin
    from backend.models import MemberRole, MembershipDB

    from .fake_chain_adapter import FakeChainAdapter

    user = bootstrap_admin(pg_session, FakeChainAdapter(), "TFakePgBootstrapAdmin00001")
    membership = pg_session.query(MembershipDB).filter(MembershipDB.user_id == user.id).first()
    assert membership.role == MemberRole.ADMIN


def test_second_bootstrap_admin_denied(pg_session):
    """POSTGRES_VERIFIED."""
    from backend.bootstrap_admin import BootstrapError, bootstrap_admin
    from backend.models import MembershipDB, MemberRole

    from .fake_chain_adapter import FakeChainAdapter

    bootstrap_admin(pg_session, FakeChainAdapter(), "TFakePgBootstrapFirst0001")
    with pytest.raises(BootstrapError, match="Ya existe al menos un ADMIN"):
        bootstrap_admin(pg_session, FakeChainAdapter(), "TFakePgBootstrapSecond001")

    assert pg_session.query(MembershipDB).filter(MembershipDB.role == MemberRole.ADMIN).count() == 1


# ---------------------------------------------------------------------------
# Phase 2B.5 — timezone-aware timestamps: PostgreSQL-specific round-trip +
# cross-dialect parity with test_time_semantics.py's SQLite-only tests
# (same service-layer functions, same relative offsets, same decision).
# ---------------------------------------------------------------------------


def test_datetime_round_trip_preserves_utc_instant(pg_session):
    """POSTGRES_VERIFIED: DateTime(timezone=True) returns a genuinely aware
    datetime on PostgreSQL (unlike SQLite, which always returns naive — see
    test_time_semantics.py) and the stored instant survives the round trip."""
    from backend import timeutils
    from backend.models import UserDB

    written = timeutils.utcnow()
    user = UserDB(wallet_address="TFakePgTzRoundTrip0001", created_at=written)
    pg_session.add(user)
    pg_session.commit()

    pg_session.expire_all()
    reloaded = pg_session.query(UserDB).filter(UserDB.wallet_address == "TFakePgTzRoundTrip0001").first()
    assert reloaded.created_at.tzinfo is not None
    assert reloaded.created_at == written


def test_nonce_expiration_same_decision_as_sqlite(pg_session):
    """POSTGRES_VERIFIED: mirrors test_time_semantics.py's nonce boundary
    tests against real Postgres — same auth.py functions, same relative
    offsets, same True/False outcome as the SQLite version."""
    from datetime import timedelta

    from backend.auth import generate_nonce, verify_and_consume_nonce
    from backend.models import AuthNonceDB

    nonce_ok = generate_nonce(pg_session, "TFakePgNonceOk0000001")
    assert verify_and_consume_nonce(pg_session, "TFakePgNonceOk0000001", nonce_ok) is True

    nonce_expired = generate_nonce(pg_session, "TFakePgNonceExpired001")
    entry = pg_session.query(AuthNonceDB).filter_by(nonce=nonce_expired).first()
    entry.expires_at = entry.expires_at - timedelta(hours=1000)
    pg_session.commit()
    assert verify_and_consume_nonce(pg_session, "TFakePgNonceExpired001", nonce_expired) is False


def test_invite_expiration_same_decision_as_sqlite(pg_session):
    """POSTGRES_VERIFIED: mirrors test_time_semantics.py's invite boundary
    tests against real Postgres — same services.invites function, same
    True/False outcome as the SQLite version."""
    from datetime import timedelta

    from backend import timeutils
    from backend.models import InviteDB, UserDB
    from backend.services.invites import InviteRedemptionError, hash_invite_code, redeem_invite_for_user

    admin = UserDB(wallet_address="TFakePgInviteAdmin0001")
    redeemer_ok = UserDB(wallet_address="TFakePgInviteOk0000001")
    redeemer_expired = UserDB(wallet_address="TFakePgInviteExp000001")
    pg_session.add_all([admin, redeemer_ok, redeemer_expired])
    pg_session.commit()

    valid_invite = InviteDB(
        code_hash=hash_invite_code("pg-code-valid"),
        created_by_user_id=admin.id,
        max_uses=1,
        used_count=0,
        expires_at=timeutils.utcnow() + timedelta(hours=1),
    )
    expired_invite = InviteDB(
        code_hash=hash_invite_code("pg-code-expired"),
        created_by_user_id=admin.id,
        max_uses=1,
        used_count=0,
        expires_at=timeutils.utcnow() - timedelta(hours=1),
    )
    pg_session.add_all([valid_invite, expired_invite])
    pg_session.commit()

    membership = redeem_invite_for_user(pg_session, "pg-code-valid", redeemer_ok)
    assert membership.user_id == redeemer_ok.id

    with pytest.raises(InviteRedemptionError):
        redeem_invite_for_user(pg_session, "pg-code-expired", redeemer_expired)
