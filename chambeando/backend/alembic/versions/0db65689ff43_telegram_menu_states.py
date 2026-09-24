"""telegram menu states

Revision ID: 0db65689ff43
Revises: a1f3c9e07b52
Create Date: 2026-09-23 12:52:52.334538

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0db65689ff43'
down_revision: Union[str, Sequence[str], None] = 'a1f3c9e07b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_STATES = (
    'IDLE', 'MENU', 'OFFER_AMOUNT', 'OFFER_CURRENCY', 'OFFER_RATE',
    'OFFER_SETTLEMENT_METHOD', 'OFFER_SETTLEMENT_LOCATION', 'OFFER_CONFIRM', 'MATCH_SELECT',
)
_NEW_STATES = ('MY_OFFERS_SELECT', 'OFFER_ACTION_SELECT', 'CANCEL_CONFIRM')
_ALL_STATES = _OLD_STATES + _NEW_STATES


def upgrade() -> None:
    """Upgrade schema. Additive-only: a1f3c9e07b52 was already applied to the
    real dev DB before these 3 states existed, so this extends the
    telegramconversationstate enum/CHECK in place rather than editing the
    already-applied revision (same pattern as a1f3c9e07b52's own
    SecurityEventType extension block)."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for state in _NEW_STATES:
            op.execute(f"ALTER TYPE telegramconversationstate ADD VALUE IF NOT EXISTS '{state}'")
    else:
        with op.batch_alter_table('telegram_sessions') as batch_op:
            batch_op.alter_column(
                'state',
                existing_type=sa.Enum(*_OLD_STATES, name='telegramconversationstate', create_constraint=True),
                type_=sa.Enum(*_ALL_STATES, name='telegramconversationstate', create_constraint=True),
            )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with op.batch_alter_table('telegram_sessions') as batch_op:
            batch_op.alter_column(
                'state',
                existing_type=sa.Enum(*_ALL_STATES, name='telegramconversationstate', create_constraint=True),
                type_=sa.Enum(*_OLD_STATES, name='telegramconversationstate', create_constraint=True),
            )
    # else: same documented PostgreSQL limitation as a1f3c9e07b52 -- no
    # ALTER TYPE ... DROP VALUE; the extra enum values stay inert after
    # downgrade as long as no row uses them.
