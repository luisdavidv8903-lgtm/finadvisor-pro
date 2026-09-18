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
