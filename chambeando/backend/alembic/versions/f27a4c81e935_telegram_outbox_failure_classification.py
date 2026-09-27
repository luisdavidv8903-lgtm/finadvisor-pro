"""telegram outbox failure classification

Revision ID: f27a4c81e935
Revises: d4b8e91f3a27
Create Date: 2026-09-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f27a4c81e935'
down_revision: Union[str, Sequence[str], None] = 'd4b8e91f3a27'
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
    'TELEGRAM_VIP_OUTBOUND_DELIVERY_UNCERTAIN', 'TELEGRAM_VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL',
)
_NEW_ACTIONS_ONLY = ('TELEGRAM_VIP_OUTBOUND_DELIVERY_FAILED_PERMANENT',)
_NEW_ACTIONS = _OLD_ACTIONS + _NEW_ACTIONS_ONLY

_OLD_STATUSES = ('PENDING', 'SENDING', 'SENT', 'FAILED_RETRYABLE', 'FAILED_TERMINAL', 'UNCERTAIN_NEEDS_RECONCILIATION')
_NEW_STATUSES = ('PENDING', 'SENDING', 'SENT', 'FAILED_RETRYABLE', 'FAILED_TERMINAL', 'FAILED_PERMANENT', 'UNCERTAIN_NEEDS_RECONCILIATION')


def upgrade() -> None:
    """Upgrade schema.

    Widens two CHECK/enum constraints d4b8e91f3a27 created, for the new
    FAILED_PERMANENT distinction (services/telegram_outbox.py's
    attempt_send_one, 2026-09-27 second pass): (1)
    security_events.action gets the new TELEGRAM_VIP_OUTBOUND_DELIVERY_
    FAILED_PERMANENT SecurityEventType member (same pattern as every prior
    enum-widening migration in this chain -- see d4b8e91f3a27's own
    docstring for why this is a SEPARATE migration step, never automatic
    from a models.py change alone). (2) telegram_outbound_messages.status
    gets the new FAILED_PERMANENT TelegramOutboundMessageStatus member."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for action in _NEW_ACTIONS_ONLY:
            op.execute(f"ALTER TYPE securityeventtype ADD VALUE IF NOT EXISTS '{action}'")
        op.execute("ALTER TYPE telegramoutboundmessagestatus ADD VALUE IF NOT EXISTS 'FAILED_PERMANENT'")
    else:
        with op.batch_alter_table('security_events') as batch_op:
            batch_op.alter_column(
                'action',
                existing_type=sa.Enum(*_OLD_ACTIONS, name='securityeventtype', create_constraint=True),
                type_=sa.Enum(*_NEW_ACTIONS, name='securityeventtype', create_constraint=True),
            )
        with op.batch_alter_table('telegram_outbound_messages') as batch_op:
            batch_op.alter_column(
                'status',
                existing_type=sa.Enum(*_OLD_STATUSES, name='telegramoutboundmessagestatus', create_constraint=True),
                type_=sa.Enum(*_NEW_STATUSES, name='telegramoutboundmessagestatus', create_constraint=True),
            )


def downgrade() -> None:
    """Downgrade schema. Postgres ADD VALUE is not reversible (same
    documented limit as every earlier enum-widening migration in this
    chain) -- only SQLite's CHECK is narrowed back here."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with op.batch_alter_table('telegram_outbound_messages') as batch_op:
            batch_op.alter_column(
                'status',
                existing_type=sa.Enum(*_NEW_STATUSES, name='telegramoutboundmessagestatus', create_constraint=True),
                type_=sa.Enum(*_OLD_STATUSES, name='telegramoutboundmessagestatus', create_constraint=True),
            )
        with op.batch_alter_table('security_events') as batch_op:
            batch_op.alter_column(
                'action',
                existing_type=sa.Enum(*_NEW_ACTIONS, name='securityeventtype', create_constraint=True),
                type_=sa.Enum(*_OLD_ACTIONS, name='securityeventtype', create_constraint=True),
            )
