"""telegram vip outbox

Revision ID: d4b8e91f3a27
Revises: c9a3f6e21b84
Create Date: 2026-09-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4b8e91f3a27'
down_revision: Union[str, Sequence[str], None] = 'c9a3f6e21b84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_ACTIONS = (
    'AUTH_FAILURE', 'AUTH_SUCCESS', 'INVITE_CREATED', 'INVITE_REDEEMED', 'INVITE_REDEMPTION_DENIED',
    'INVITE_REVOKED', 'MEMBERSHIP_SUSPENDED', 'MEMBERSHIP_REACTIVATED', 'ROLE_CHANGED',
    'SETTLEMENT_DETAIL_VIEWED', 'SETTLEMENT_DETAIL_CREATED', 'SETTLEMENT_DETAIL_REVOKED',
    'DISPUTE_EVIDENCE_VIEWED', 'DISPUTE_EVIDENCE_SUBMITTED', 'DISPUTE_ASSIGNMENT_CREATED',
    'DISPUTE_ASSIGNMENT_REVOKED', 'REPORT_FILED', 'REPORT_REVIEWED', 'RATE_LIMIT_TRIGGERED',
    'ADMIN_BOOTSTRAPPED', 'WHATSAPP_ACCOUNT_LINKED', 'TELEGRAM_IDENTITY_REGISTERED',
    'TELEGRAM_MEMBERSHIP_SUSPENDED', 'TELEGRAM_MEMBERSHIP_REACTIVATED', 'TELEGRAM_OFFER_CREATED',
    'TELEGRAM_OFFER_MATCHED', 'TELEGRAM_OFFER_STATUS_CHANGED',
    'VIP_WHATSAPP_ID_AUTHORIZED', 'VIP_INVITE_CREATED', 'VIP_INVITE_REDEEMED', 'VIP_MEMBERSHIP_REVOKED',
    'VIP_OFFER_CREATED', 'VIP_OFFER_CLOSED', 'VIP_OFFER_INTEREST_REGISTERED',
    'VIP_OUTBOUND_DELIVERY_UNCERTAIN', 'VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL',
)
_NEW_ACTIONS_ONLY = ('TELEGRAM_VIP_OUTBOUND_DELIVERY_UNCERTAIN', 'TELEGRAM_VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL')
_NEW_ACTIONS = _OLD_ACTIONS + _NEW_ACTIONS_ONLY


def upgrade() -> None:
    """Upgrade schema.

    Two parts: (1) extend the existing security_events.action CHECK/enum
    with the two new SecurityEventType members this outbox logs (same
    Postgres-native-type-vs-SQLite-CHECK pattern as c9a3f6e21b84/
    a1f3c9e07b52/989d2c51d5ca -- a new enum member added in models.py does
    NOT retroactively widen a CHECK constraint a real `alembic upgrade
    head` already produced; see tests/test_migrations.py's
    test_every_security_event_type_member_is_insertable_on_a_migrated_db,
    which is exactly what caught this being missing here). (2) ONE new
    table -- telegram_outbound_messages (the Telegram counterpart of
    whatsapp_outbound_messages, added by c9a3f6e21b84). No new lease table:
    the Telegram outbox reuses whatsapp_outbox_worker_lease (already
    created by c9a3f6e21b84) with a second row, id=2 -- see
    OutboxWorkerLeaseDB's model docstring. That row is created lazily by
    services/telegram_outbox.py's _ensure_lease_row on first use, never by
    this migration (same as WhatsApp's own id=1 row, which c9a3f6e21b84
    also never seeds)."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for action in _NEW_ACTIONS_ONLY:
            op.execute(f"ALTER TYPE securityeventtype ADD VALUE IF NOT EXISTS '{action}'")
    else:
        with op.batch_alter_table('security_events') as batch_op:
            batch_op.alter_column(
                'action',
                existing_type=sa.Enum(*_OLD_ACTIONS, name='securityeventtype', create_constraint=True),
                type_=sa.Enum(*_NEW_ACTIONS, name='securityeventtype', create_constraint=True),
            )

    op.create_table('telegram_outbound_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('telegram_chat_id', sa.String(), nullable=False),
    sa.Column('body_text', sa.String(), nullable=False),
    sa.Column(
        'status',
        sa.Enum('PENDING', 'SENDING', 'SENT', 'FAILED_RETRYABLE', 'FAILED_TERMINAL', 'UNCERTAIN_NEEDS_RECONCILIATION', name='telegramoutboundmessagestatus', create_constraint=True),
        nullable=False,
    ),
    sa.Column('attempt_count', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.String(), nullable=True),
    sa.Column('source_message_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_telegram_outbound_messages_id'), 'telegram_outbound_messages', ['id'], unique=False)
    op.create_index(op.f('ix_telegram_outbound_messages_telegram_chat_id'), 'telegram_outbound_messages', ['telegram_chat_id'], unique=False)
    op.create_index(op.f('ix_telegram_outbound_messages_status'), 'telegram_outbound_messages', ['status'], unique=False)
    op.create_index(op.f('ix_telegram_outbound_messages_source_message_id'), 'telegram_outbound_messages', ['source_message_id'], unique=False)
    op.create_index(op.f('ix_telegram_outbound_messages_next_attempt_at'), 'telegram_outbound_messages', ['next_attempt_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_telegram_outbound_messages_next_attempt_at'), table_name='telegram_outbound_messages')
    op.drop_index(op.f('ix_telegram_outbound_messages_source_message_id'), table_name='telegram_outbound_messages')
    op.drop_index(op.f('ix_telegram_outbound_messages_status'), table_name='telegram_outbound_messages')
    op.drop_index(op.f('ix_telegram_outbound_messages_telegram_chat_id'), table_name='telegram_outbound_messages')
    op.drop_index(op.f('ix_telegram_outbound_messages_id'), table_name='telegram_outbound_messages')
    op.drop_table('telegram_outbound_messages')

    # Postgres ADD VALUE is not reversible (same documented limit as every
    # earlier enum-widening migration in this chain) -- only SQLite's CHECK
    # is narrowed back here.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with op.batch_alter_table('security_events') as batch_op:
            batch_op.alter_column(
                'action',
                existing_type=sa.Enum(*_NEW_ACTIONS, name='securityeventtype', create_constraint=True),
                type_=sa.Enum(*_OLD_ACTIONS, name='securityeventtype', create_constraint=True),
            )
