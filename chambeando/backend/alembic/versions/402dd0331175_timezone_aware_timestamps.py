"""timezone-aware timestamps

Phase 2B.5: every DateTime column moves from naive to timezone-aware
(DateTime(timezone=True) / TIMESTAMP WITH TIME ZONE). PostgreSQL's driver
returns genuinely aware datetimes for this type; SQLite still returns naive
datetimes regardless (driver limitation — verified empirically), so the
column-type change alone does not make SQLite reads aware. The application
normalizes with backend.timeutils.ensure_utc() wherever a value read from
the DB is compared in Python. No real user data exists yet — all existing
test rows are assumed UTC (the only value the app has ever written), so
Postgres's USING clause reinterprets the stored naive wall-clock value as
UTC rather than converting a timezone.

Revision ID: 402dd0331175
Revises: 9355077ef709
Create Date: 2026-09-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '402dd0331175'
down_revision: Union[str, Sequence[str], None] = '9355077ef709'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column) for all 19 DateTime columns in models.py
_COLUMNS = [
    ("auth_nonces", "expires_at"),
    ("auth_nonces", "created_at"),
    ("users", "created_at"),
    ("invites", "expires_at"),
    ("invites", "revoked_at"),
    ("invites", "created_at"),
    ("memberships", "joined_at"),
    ("memberships", "suspended_at"),
    ("settlement_details", "created_at"),
    ("settlement_details", "revoked_at"),
    ("p2p_orders", "created_at"),
    ("dispute_evidence", "created_at"),
    ("dispute_assignments", "created_at"),
    ("dispute_assignments", "expires_at"),
    ("dispute_assignments", "revoked_at"),
    ("arbiter_resolutions", "created_at"),
    ("reports", "created_at"),
    ("reports", "reviewed_at"),
    ("security_events", "created_at"),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # existing naive values were always written as UTC wall-clock by the
        # app (datetime.now(timezone.utc), stripped of tzinfo by the old
        # naive column type) -> reinterpret, not convert, as UTC.
        for table, column in _COLUMNS:
            op.execute(
                f'ALTER TABLE {table} ALTER COLUMN {column} '
                f"TYPE TIMESTAMP WITH TIME ZONE USING {column} AT TIME ZONE 'UTC'"
            )
    else:
        # SQLite has no real ALTER COLUMN TYPE and does not distinguish
        # tz-aware storage anyway (values are stored as ISO strings either
        # way) -> batch-recreate per table so the declared schema matches
        # models.py, data untouched.
        tables: dict[str, list[str]] = {}
        for table, column in _COLUMNS:
            tables.setdefault(table, []).append(column)
        for table, columns in tables.items():
            with op.batch_alter_table(table) as batch_op:
                for column in columns:
                    batch_op.alter_column(
                        column,
                        existing_type=sa.DateTime(),
                        type_=sa.DateTime(timezone=True),
                    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table, column in _COLUMNS:
            op.execute(
                f'ALTER TABLE {table} ALTER COLUMN {column} '
                f"TYPE TIMESTAMP WITHOUT TIME ZONE USING {column} AT TIME ZONE 'UTC'"
            )
    else:
        tables: dict[str, list[str]] = {}
        for table, column in _COLUMNS:
            tables.setdefault(table, []).append(column)
        for table, columns in tables.items():
            with op.batch_alter_table(table) as batch_op:
                for column in columns:
                    batch_op.alter_column(
                        column,
                        existing_type=sa.DateTime(timezone=True),
                        type_=sa.DateTime(),
                    )
