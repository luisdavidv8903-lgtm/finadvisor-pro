"""telegram v1 pilot tables

Revision ID: a1f3c9e07b52
Revises: 989d2c51d5ca
Create Date: 2026-09-22 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f3c9e07b52'
down_revision: Union[str, Sequence[str], None] = '989d2c51d5ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('channel_identities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.Enum('TELEGRAM', name='channel', create_constraint=True), nullable=False),
    sa.Column('channel_user_id', sa.String(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel', 'channel_user_id', name='uq_channel_identity')
    )
    op.create_index(op.f('ix_channel_identities_id'), 'channel_identities', ['id'], unique=False)
    op.create_index(op.f('ix_channel_identities_channel_user_id'), 'channel_identities', ['channel_user_id'], unique=False)
    op.create_index(op.f('ix_channel_identities_user_id'), 'channel_identities', ['user_id'], unique=False)

    op.create_table('telegram_memberships',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_identity_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.Enum('MEMBER', 'MODERATOR', 'ADMIN', name='memberrole', create_constraint=True), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'SUSPENDED', name='membershipstatus', create_constraint=True), nullable=False),
    sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('suspended_by_channel_identity_id', sa.Integer(), nullable=True),
    sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('suspension_reason', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['channel_identity_id'], ['channel_identities.id'], ),
    sa.ForeignKeyConstraint(['suspended_by_channel_identity_id'], ['channel_identities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel_identity_id', name='uq_telegram_membership_identity')
    )
    op.create_index(op.f('ix_telegram_memberships_id'), 'telegram_memberships', ['id'], unique=False)
    op.create_index(op.f('ix_telegram_memberships_channel_identity_id'), 'telegram_memberships', ['channel_identity_id'], unique=False)

    op.create_table('p2p_offers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_identity_id', sa.Integer(), nullable=False),
    sa.Column('side', sa.Enum('COMPRO', 'VENDO', name='offerside', create_constraint=True), nullable=False),
    sa.Column('amount', sa.Numeric(18, 2), nullable=False),
    sa.Column('currency', sa.String(), nullable=False),
    sa.Column('rate', sa.Numeric(18, 4), nullable=False),
    sa.Column('settlement_method', sa.String(), nullable=False),
    sa.Column('settlement_location', sa.String(), nullable=False),
    sa.Column('status', sa.Enum('OPEN', 'MATCHED', 'PAYMENT_PENDING', 'COMPLETED', 'CANCELLED', 'DISPUTED', name='offerstatus', create_constraint=True), nullable=False),
    sa.Column('matched_offer_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['channel_identity_id'], ['channel_identities.id'], ),
    sa.ForeignKeyConstraint(['matched_offer_id'], ['p2p_offers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_p2p_offers_id'), 'p2p_offers', ['id'], unique=False)
    op.create_index(op.f('ix_p2p_offers_channel_identity_id'), 'p2p_offers', ['channel_identity_id'], unique=False)
    op.create_index(op.f('ix_p2p_offers_status'), 'p2p_offers', ['status'], unique=False)

    op.create_table('telegram_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_identity_id', sa.Integer(), nullable=False),
    sa.Column(
        'state',
        sa.Enum(
            'IDLE', 'MENU', 'OFFER_AMOUNT', 'OFFER_CURRENCY', 'OFFER_RATE',
            'OFFER_SETTLEMENT_METHOD', 'OFFER_SETTLEMENT_LOCATION', 'OFFER_CONFIRM', 'MATCH_SELECT',
            name='telegramconversationstate', create_constraint=True,
        ),
        nullable=False,
    ),
    sa.Column('context', sa.String(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['channel_identity_id'], ['channel_identities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel_identity_id')
    )
    op.create_index(op.f('ix_telegram_sessions_id'), 'telegram_sessions', ['id'], unique=False)

    # SecurityEventType extension -- same Postgres-native-type-vs-SQLite-CHECK
    # gotcha documented in 989d2c51d5ca: adding Python enum members never
    # retroactively updates an already-created constraint/type.
    _OLD_ACTIONS = (
        'AUTH_FAILURE', 'AUTH_SUCCESS', 'INVITE_CREATED', 'INVITE_REDEEMED', 'INVITE_REDEMPTION_DENIED',
        'INVITE_REVOKED', 'MEMBERSHIP_SUSPENDED', 'MEMBERSHIP_REACTIVATED', 'ROLE_CHANGED',
        'SETTLEMENT_DETAIL_VIEWED', 'SETTLEMENT_DETAIL_CREATED', 'SETTLEMENT_DETAIL_REVOKED',
        'DISPUTE_EVIDENCE_VIEWED', 'DISPUTE_EVIDENCE_SUBMITTED', 'DISPUTE_ASSIGNMENT_CREATED',
        'DISPUTE_ASSIGNMENT_REVOKED', 'REPORT_FILED', 'REPORT_REVIEWED', 'RATE_LIMIT_TRIGGERED',
        'ADMIN_BOOTSTRAPPED', 'WHATSAPP_ACCOUNT_LINKED',
    )
    _TELEGRAM_ACTIONS = (
        'TELEGRAM_IDENTITY_REGISTERED', 'TELEGRAM_MEMBERSHIP_SUSPENDED', 'TELEGRAM_MEMBERSHIP_REACTIVATED',
        'TELEGRAM_OFFER_CREATED', 'TELEGRAM_OFFER_MATCHED', 'TELEGRAM_OFFER_STATUS_CHANGED',
    )
    _NEW_ACTIONS = _OLD_ACTIONS + _TELEGRAM_ACTIONS

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for action in _TELEGRAM_ACTIONS:
            op.execute(f"ALTER TYPE securityeventtype ADD VALUE IF NOT EXISTS '{action}'")
    else:
        with op.batch_alter_table('security_events') as batch_op:
            batch_op.alter_column(
                'action',
                existing_type=sa.Enum(*_OLD_ACTIONS, name='securityeventtype', create_constraint=True),
                type_=sa.Enum(*_NEW_ACTIONS, name='securityeventtype', create_constraint=True),
            )


def downgrade() -> None:
    """Downgrade schema."""
    _OLD_ACTIONS = (
        'AUTH_FAILURE', 'AUTH_SUCCESS', 'INVITE_CREATED', 'INVITE_REDEEMED', 'INVITE_REDEMPTION_DENIED',
        'INVITE_REVOKED', 'MEMBERSHIP_SUSPENDED', 'MEMBERSHIP_REACTIVATED', 'ROLE_CHANGED',
        'SETTLEMENT_DETAIL_VIEWED', 'SETTLEMENT_DETAIL_CREATED', 'SETTLEMENT_DETAIL_REVOKED',
        'DISPUTE_EVIDENCE_VIEWED', 'DISPUTE_EVIDENCE_SUBMITTED', 'DISPUTE_ASSIGNMENT_CREATED',
        'DISPUTE_ASSIGNMENT_REVOKED', 'REPORT_FILED', 'REPORT_REVIEWED', 'RATE_LIMIT_TRIGGERED',
        'ADMIN_BOOTSTRAPPED', 'WHATSAPP_ACCOUNT_LINKED',
    )
    _TELEGRAM_ACTIONS = (
        'TELEGRAM_IDENTITY_REGISTERED', 'TELEGRAM_MEMBERSHIP_SUSPENDED', 'TELEGRAM_MEMBERSHIP_REACTIVATED',
        'TELEGRAM_OFFER_CREATED', 'TELEGRAM_OFFER_MATCHED', 'TELEGRAM_OFFER_STATUS_CHANGED',
    )
    _NEW_ACTIONS = _OLD_ACTIONS + _TELEGRAM_ACTIONS

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with op.batch_alter_table('security_events') as batch_op:
            batch_op.alter_column(
                'action',
                existing_type=sa.Enum(*_NEW_ACTIONS, name='securityeventtype', create_constraint=True),
                type_=sa.Enum(*_OLD_ACTIONS, name='securityeventtype', create_constraint=True),
            )
    # else: same documented PostgreSQL limitation as 989d2c51d5ca -- no
    # ALTER TYPE ... DROP VALUE; the extra enum values stay inert in the
    # native type after downgrade (no row can use them once the Telegram
    # tables are dropped below).

    op.drop_index(op.f('ix_telegram_sessions_id'), table_name='telegram_sessions')
    op.drop_table('telegram_sessions')
    op.drop_index(op.f('ix_p2p_offers_status'), table_name='p2p_offers')
    op.drop_index(op.f('ix_p2p_offers_channel_identity_id'), table_name='p2p_offers')
    op.drop_index(op.f('ix_p2p_offers_id'), table_name='p2p_offers')
    op.drop_table('p2p_offers')
    op.drop_index(op.f('ix_telegram_memberships_channel_identity_id'), table_name='telegram_memberships')
    op.drop_index(op.f('ix_telegram_memberships_id'), table_name='telegram_memberships')
    op.drop_table('telegram_memberships')
    op.drop_index(op.f('ix_channel_identities_user_id'), table_name='channel_identities')
    op.drop_index(op.f('ix_channel_identities_channel_user_id'), table_name='channel_identities')
    op.drop_index(op.f('ix_channel_identities_id'), table_name='channel_identities')
    op.drop_table('channel_identities')

    # Same native-Postgres-TYPE cleanup gotcha as 989d2c51d5ca: DROP TABLE
    # never removes the separate TYPE objects create_constraint=True made.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS telegramconversationstate")
        op.execute("DROP TYPE IF EXISTS offerstatus")
        op.execute("DROP TYPE IF EXISTS offerside")
        op.execute("DROP TYPE IF EXISTS channel")
