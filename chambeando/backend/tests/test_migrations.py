"""
Phase 2B.1 #4 — Alembic migrations, not create_all(), is the production schema
strategy (see backend/alembic/). These tests exercise the REAL migration
files against a fresh sqlite file: empty -> head, verify structure, downgrade,
upgrade again.
"""

import os
import pathlib
import sqlite3

import pytest
from alembic import command
from alembic.config import Config

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture()
def alembic_config(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg, db_path


def _table_names(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _columns(db_path, table) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    finally:
        conn.close()


def _index_names(db_path, table) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
        return {r[1] for r in rows}
    finally:
        conn.close()


EXPECTED_TABLES = {
    "users",
    "auth_nonces",
    "invites",
    "memberships",
    "settlement_details",
    "p2p_orders",
    "dispute_evidence",
    "dispute_assignments",
    "arbiter_resolutions",
    "indexer_checkpoints",
    "reports",
    "security_events",
}


def test_upgrade_empty_db_to_head(alembic_config):
    cfg, db_path = alembic_config
    assert not db_path.exists()
    command.upgrade(cfg, "head")
    assert db_path.exists()
    assert EXPECTED_TABLES.issubset(_table_names(db_path))


def test_required_columns_and_indexes_present(alembic_config):
    cfg, db_path = alembic_config
    command.upgrade(cfg, "head")

    assert {"id", "wallet_address", "alias", "created_at"} <= _columns(db_path, "users")
    assert {"id", "code_hash", "max_uses", "used_count", "expires_at", "revoked_at"} <= _columns(db_path, "invites")
    assert {"id", "user_id", "role", "status", "suspended_reason"} <= _columns(db_path, "memberships")
    assert {"id", "owner_user_id", "encrypted_payload", "active"} <= _columns(db_path, "settlement_details")
    assert {"id", "onchain_order_id", "onchain_status", "arbiter_snapshot_wallet", "was_disputed", "settlement_detail_id"} <= _columns(
        db_path, "p2p_orders"
    )
    assert {"id", "order_id", "assigned_user_id", "assigned_by_user_id", "expires_at", "revoked_at"} <= _columns(
        db_path, "dispute_assignments"
    )

    assert "ix_users_wallet_address" in _index_names(db_path, "users")
    assert "ix_invites_code_hash" in _index_names(db_path, "invites")
    assert "ix_p2p_orders_onchain_order_id" in _index_names(db_path, "p2p_orders")


def test_downgrade_from_head_to_base(alembic_config):
    cfg, db_path = alembic_config
    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES.issubset(_table_names(db_path))

    command.downgrade(cfg, "base")
    remaining = _table_names(db_path)
    assert not (EXPECTED_TABLES & remaining), f"tables survived downgrade: {EXPECTED_TABLES & remaining}"


def test_upgrade_after_downgrade_reapplies_cleanly(alembic_config):
    cfg, db_path = alembic_config
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES.issubset(_table_names(db_path))


def test_unique_constraints_present_in_ddl(alembic_config):
    cfg, db_path = alembic_config
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        ddl = {
            row[0]: row[1]
            for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()

    assert "UNIQUE" in ddl["users"]  # wallet_address (via index, see below) + alias
    assert "ix_users_wallet_address" in _index_names(db_path, "users")
    assert "uq_membership_user UNIQUE (user_id)" in ddl["memberships"]
    assert "code_hash" in ddl["invites"]
    assert "uq_order_onchain_id UNIQUE (onchain_order_id)" in ddl["p2p_orders"]
    assert "FOREIGN KEY(owner_user_id) REFERENCES users (id)" in ddl["settlement_details"]
    assert "FOREIGN KEY(order_id) REFERENCES p2p_orders (id)" in ddl["dispute_evidence"]
    assert "FOREIGN KEY(order_id) REFERENCES p2p_orders (id)" in ddl["dispute_assignments"]


def test_enum_columns_have_db_level_check_constraints(alembic_config):
    """Phase 2B.1: role/status/onchain_status/action ahora tienen CHECK a nivel
    de DB (create_constraint=True en models.py), no solo validacion Python —
    ver ENFORCEMENT_LEVELS.md."""
    cfg, db_path = alembic_config
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        memberships_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='memberships'").fetchone()[0]
        orders_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='p2p_orders'").fetchone()[0]
        events_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='security_events'").fetchone()[0]
    finally:
        conn.close()

    assert "CHECK" in memberships_sql and "MODERATOR" in memberships_sql
    assert "CHECK" in orders_sql and "DISPUTED" in orders_sql
    assert "CHECK" in events_sql

    # y ademas se rechaza a nivel de motor, no solo declarado — un INSERT crudo
    # con un valor invalido debe fallar
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users (wallet_address) VALUES ('Tsynthetic00000000000000000001')"
            )
            conn.execute(
                "INSERT INTO memberships (user_id, role, status) VALUES (1, 'NOT_A_REAL_ROLE', 'ACTIVE')"
            )
            conn.commit()
    finally:
        conn.close()


def test_running_a_migration_does_not_disable_preexisting_app_loggers(alembic_config):
    """Regression test for a real Phase 2D.1 bug: alembic/env.py's
    `fileConfig(config.config_file_name)` defaulted to
    `disable_existing_loggers=True` (the stdlib default), which silently set
    `.disabled = True` on every `chambeando.*` logger already created before
    the migration ran -- e.g. `chambeando.whatsapp.meta_client` -- dropping
    every future `.error()`/`.info()` call on it as a no-op for the rest of
    the process, regardless of level/propagation/handlers. Found via a real
    test failure (test_meta_cloud_client.py's sanitized-logging test passed
    alone but failed whenever it ran after ANY test that had already
    triggered a real `alembic upgrade head`). Fixed by passing
    `disable_existing_loggers=False`."""
    import logging

    logger_name = "chambeando.some_preexisting_test_logger"
    logger = logging.getLogger(logger_name)
    assert logger.disabled is False

    cfg, db_path = alembic_config
    command.upgrade(cfg, "head")

    assert logger.disabled is False


def test_legacy_users_and_nonces_survive_multi_network_migration(alembic_config):
    """Revision 9229f715361d (autenticacion multi-red, 2026-09-25) agrega
    `network_id` a `users`/`auth_nonces` y cambia la unicidad de
    `wallet_address` sola a (network_id, wallet_address). Este test simula
    datos HEREDADOS reales: filas insertadas cuando esas columnas todavia no
    existian (upgrade solo hasta la revision ANTERIOR, 8834a2893229), y
    verifica tres propiedades explicitamente pedidas en la revision humana:

    1. La migracion asigna 'tron' a usuarios/nonces heredados (nunca los deja
       NULL, nunca falla por NOT NULL sin default).
    2. No se crean filas duplicadas ni se pierden vinculos: mismo `id` de
       usuario antes y despues, y una fila de `memberships` que ya apuntaba a
       ese `user_id` (FK) lo sigue encontrando exactamente igual.
    3. No se invalidan sesiones existentes por accidente: un JWT con la forma
       VIEJA (sin claim `network_id`, como los que emitia el codigo antes de
       esta revision) sigue resolviendo al MISMO usuario via el mismo
       fallback que auth.get_current_user aplica (LEGACY_NETWORK_ID_FALLBACK
       = "tron"), sin crear un usuario duplicado."""
    import sqlite3

    from jose import jwt as jose_jwt
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.auth import LEGACY_NETWORK_ID_FALLBACK
    from backend.config import settings
    from backend.models import UserDB

    cfg, db_path = alembic_config

    # 1) Upgrade SOLO hasta la revision anterior -- 'network_id' todavia no existe.
    command.upgrade(cfg, "8834a2893229")
    assert "network_id" not in _columns(db_path, "users")
    assert "network_id" not in _columns(db_path, "auth_nonces")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, wallet_address, alias, created_at) VALUES (?, ?, ?, ?)",
            (1, "TLegacyRealUserBeforeMigration01", None, "2026-01-01 00:00:00"),
        )
        conn.execute(
            "INSERT INTO memberships (id, user_id, role, status, joined_at) VALUES (?, ?, ?, ?, ?)",
            (1, 1, "MEMBER", "ACTIVE", "2026-01-01 00:00:00"),
        )
        conn.execute(
            "INSERT INTO auth_nonces (id, wallet_address, nonce, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "TLegacyRealUserBeforeMigration01", "deadbeefcafef00d0legacynonce001", "2099-01-01 00:00:00", 0, "2026-01-01 00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    # 2) Upgrade a head -- aplica la migracion multi-red sobre datos ya presentes.
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db_path)
    try:
        users = conn.execute("SELECT id, wallet_address, network_id FROM users").fetchall()
        nonces = conn.execute("SELECT id, wallet_address, network_id, nonce FROM auth_nonces").fetchall()
        memberships = conn.execute("SELECT id, user_id FROM memberships").fetchall()
    finally:
        conn.close()

    # -- ninguna fila se duplico --
    assert len(users) == 1, f"se esperaba exactamente 1 usuario heredado, hay {len(users)}: {users}"
    assert len(nonces) == 1, f"se esperaba exactamente 1 nonce heredado, hay {len(nonces)}: {nonces}"
    assert len(memberships) == 1

    # -- 'tron' asignado automaticamente, nunca NULL --
    assert users[0] == (1, "TLegacyRealUserBeforeMigration01", "tron")
    assert nonces[0] == (1, "TLegacyRealUserBeforeMigration01", "tron", "deadbeefcafef00d0legacynonce001")

    # -- el vinculo (FK) de membership al mismo user_id sigue intacto --
    assert memberships[0] == (1, 1)

    # 3) Una sesion (JWT) emitida ANTES de esta revision, sin claim network_id,
    # debe seguir resolviendo al MISMO usuario -- nunca crear un duplicado ni
    # rechazar la sesion.
    legacy_jwt = jose_jwt.encode({"sub": "TLegacyRealUserBeforeMigration01"}, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    decoded = jose_jwt.decode(legacy_jwt, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert "network_id" not in decoded  # exactamente la forma vieja
    resolved_network_id = decoded.get("network_id") or LEGACY_NETWORK_ID_FALLBACK
    assert resolved_network_id == "tron"

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        matched = (
            session.query(UserDB)
            .filter(UserDB.wallet_address == decoded["sub"], UserDB.network_id == resolved_network_id)
            .all()
        )
        assert len(matched) == 1, "el JWT legado debe resolver a exactamente el usuario heredado, sin duplicarlo"
        assert matched[0].id == 1
    finally:
        session.close()
        engine.dispose()


def test_downgrade_fails_with_a_clear_diagnostic_when_two_networks_share_a_wallet_address(alembic_config):
    """Hallazgo de revision (2026-09-25): la unicidad compuesta
    (network_id, wallet_address) permite deliberadamente que la misma
    wallet_address exista en redes distintas -- pero el downgrade de
    9229f715361d reinstala una unicidad GLOBAL de wallet_address. Simula
    ese escenario (sintetico: TRON/EVM nunca comparten formato de
    direccion en la practica hoy, pero la migracion debe manejarlo sin
    fusionar ni borrar nada en silencio) y confirma que el downgrade FALLA
    con un mensaje explicito, nombrando la wallet_address y las redes en
    conflicto -- nunca un IntegrityError crudo, nunca una fusion/borrado
    automatico."""
    cfg, db_path = alembic_config
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, wallet_address, network_id, created_at) VALUES (1, 'SharedAddressAcrossNetworks01', 'tron', '2026-01-01 00:00:00')"
        )
        conn.execute(
            "INSERT INTO users (id, wallet_address, network_id, created_at) VALUES (2, 'SharedAddressAcrossNetworks01', 'bsc', '2026-01-01 00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError) as excinfo:
        command.downgrade(cfg, "8834a2893229")

    message = str(excinfo.value)
    assert "SharedAddressAcrossNetworks01" in message
    assert "'tron'" in message and "'bsc'" in message
    assert "no se puede revertir" in message.lower()

    # nada se toco: ni se fusiono, ni se borro, ni el esquema cambio -- el
    # chequeo corre ANTES de cualquier DDL.
    assert "network_id" in _columns(db_path, "users")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, wallet_address, network_id FROM users WHERE wallet_address='SharedAddressAcrossNetworks01'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert {r[2] for r in rows} == {"tron", "bsc"}


def test_every_security_event_type_member_is_insertable_on_a_migrated_db(alembic_config):
    """Regression test for a real Phase 2C bug: adding a new
    SecurityEventType member in models.py does NOT retroactively update the
    CHECK constraint a real `alembic upgrade head` produces (that constraint
    was frozen with its original member list back in the initial migration,
    and only a dedicated migration step can widen it) — invisible against
    create_all(), which always regenerates the CHECK from the current model.
    This caught WHATSAPP_ACCOUNT_LINKED being rejected until migration
    989d2c51d5ca explicitly added it on both dialects."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.models import SecurityEventDB, SecurityEventType, UserDB

    cfg, db_path = alembic_config
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        user = UserDB(wallet_address="Tsynthetic0EnumDriftCheck00001")
        session.add(user)
        session.commit()
        for action in SecurityEventType:
            session.add(SecurityEventDB(actor_user_id=user.id, action=action))
            session.commit()
    finally:
        session.close()
        engine.dispose()
