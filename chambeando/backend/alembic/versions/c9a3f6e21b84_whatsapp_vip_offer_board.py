"""whatsapp vip offer board

Revision ID: c9a3f6e21b84
Revises: 9229f715361d
Create Date: 2026-09-26 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9a3f6e21b84'
down_revision: Union[str, Sequence[str], None] = '9229f715361d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- Extend the existing `channel` enum/CHECK with WHATSAPP -- same
    # Postgres-native-type-vs-SQLite-CHECK gotcha as a1f3c9e07b52/0db65689ff43.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE channel ADD VALUE IF NOT EXISTS 'WHATSAPP'")
    else:
        with op.batch_alter_table('channel_identities') as batch_op:
            batch_op.alter_column(
                'channel',
                existing_type=sa.Enum('TELEGRAM', name='channel', create_constraint=True),
                type_=sa.Enum('TELEGRAM', 'WHATSAPP', name='channel', create_constraint=True),
            )

    # --- SecurityEventType extension -- same pattern as a1f3c9e07b52.
    _OLD_ACTIONS = (
        'AUTH_FAILURE', 'AUTH_SUCCESS', 'INVITE_CREATED', 'INVITE_REDEEMED', 'INVITE_REDEMPTION_DENIED',
        'INVITE_REVOKED', 'MEMBERSHIP_SUSPENDED', 'MEMBERSHIP_REACTIVATED', 'ROLE_CHANGED',
        'SETTLEMENT_DETAIL_VIEWED', 'SETTLEMENT_DETAIL_CREATED', 'SETTLEMENT_DETAIL_REVOKED',
        'DISPUTE_EVIDENCE_VIEWED', 'DISPUTE_EVIDENCE_SUBMITTED', 'DISPUTE_ASSIGNMENT_CREATED',
        'DISPUTE_ASSIGNMENT_REVOKED', 'REPORT_FILED', 'REPORT_REVIEWED', 'RATE_LIMIT_TRIGGERED',
        'ADMIN_BOOTSTRAPPED', 'WHATSAPP_ACCOUNT_LINKED', 'TELEGRAM_IDENTITY_REGISTERED',
        'TELEGRAM_MEMBERSHIP_SUSPENDED', 'TELEGRAM_MEMBERSHIP_REACTIVATED', 'TELEGRAM_OFFER_CREATED',
        'TELEGRAM_OFFER_MATCHED', 'TELEGRAM_OFFER_STATUS_CHANGED',
    )
    _VIP_ACTIONS = (
        'VIP_WHATSAPP_ID_AUTHORIZED', 'VIP_INVITE_CREATED', 'VIP_INVITE_REDEEMED', 'VIP_MEMBERSHIP_REVOKED',
        'VIP_OFFER_CREATED', 'VIP_OFFER_CLOSED', 'VIP_OFFER_INTEREST_REGISTERED',
        'VIP_OUTBOUND_DELIVERY_UNCERTAIN', 'VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL',
    )
    _NEW_ACTIONS = _OLD_ACTIONS + _VIP_ACTIONS

    if bind.dialect.name == "postgresql":
        for action in _VIP_ACTIONS:
            op.execute(f"ALTER TYPE securityeventtype ADD VALUE IF NOT EXISTS '{action}'")
    else:
        with op.batch_alter_table('security_events') as batch_op:
            batch_op.alter_column(
                'action',
                existing_type=sa.Enum(*_OLD_ACTIONS, name='securityeventtype', create_constraint=True),
                type_=sa.Enum(*_NEW_ACTIONS, name='securityeventtype', create_constraint=True),
            )

    # --- New tables -----------------------------------------------------

    op.create_table('whatsapp_vip_invites',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code_hash', sa.String(), nullable=False),
    sa.Column('max_uses', sa.Integer(), nullable=False),
    sa.Column('used_count', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('note', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code_hash')
    )
    op.create_index(op.f('ix_whatsapp_vip_invites_id'), 'whatsapp_vip_invites', ['id'], unique=False)
    op.create_index(op.f('ix_whatsapp_vip_invites_code_hash'), 'whatsapp_vip_invites', ['code_hash'], unique=False)

    op.create_table('whatsapp_vip_memberships',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_identity_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'SUSPENDED', name='membershipstatus', create_constraint=True), nullable=False),
    sa.Column('authorized_via', sa.Enum('DIRECT', 'INVITE', name='vipauthorizationmethod', create_constraint=True), nullable=False),
    sa.Column('invite_id', sa.Integer(), nullable=True),
    sa.Column('authorized_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoke_reason', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['channel_identity_id'], ['channel_identities.id'], ),
    sa.ForeignKeyConstraint(['invite_id'], ['whatsapp_vip_invites.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel_identity_id', name='uq_vip_membership_identity')
    )
    op.create_index(op.f('ix_whatsapp_vip_memberships_id'), 'whatsapp_vip_memberships', ['id'], unique=False)
    op.create_index(op.f('ix_whatsapp_vip_memberships_channel_identity_id'), 'whatsapp_vip_memberships', ['channel_identity_id'], unique=False)

    op.create_table('whatsapp_vip_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_identity_id', sa.Integer(), nullable=False),
    sa.Column(
        'state',
        sa.Enum(
            'IDLE', 'WIZ_INTENT', 'WIZ_GIVE_CURRENCY', 'WIZ_RECEIVE_CURRENCY', 'WIZ_AMOUNT',
            'WIZ_RATE_UNIT', 'WIZ_RATE_VALUE', 'WIZ_GIVE_METHOD', 'WIZ_GIVE_METHOD_OTHER', 'WIZ_GIVE_LOCATION',
            'WIZ_RECEIVE_METHOD', 'WIZ_RECEIVE_METHOD_OTHER', 'WIZ_RECEIVE_LOCATION', 'WIZ_DURATION', 'WIZ_CONFIRM',
            name='vipwizardstate', create_constraint=True,
        ),
        nullable=False,
    ),
    sa.Column('context', sa.String(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['channel_identity_id'], ['channel_identities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel_identity_id')
    )
    op.create_index(op.f('ix_whatsapp_vip_sessions_id'), 'whatsapp_vip_sessions', ['id'], unique=False)

    op.create_table('whatsapp_vip_offers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_identity_id', sa.Integer(), nullable=False),
    sa.Column('intent', sa.Enum('COMPRO', 'VENDO', 'CAMBIO', name='vipofferintent', create_constraint=True), nullable=False),
    sa.Column('give_currency', sa.Enum('USD', 'CUP', 'EUR', 'MLC', 'MXN', name='vipcurrency', create_constraint=True), nullable=False),
    sa.Column('receive_currency', sa.Enum('USD', 'CUP', 'EUR', 'MLC', 'MXN', name='vipcurrency', create_constraint=True), nullable=False),
    sa.Column('amount', sa.Numeric(18, 2), nullable=False),
    sa.Column('min_amount', sa.Numeric(18, 2), nullable=True),
    sa.Column('rate_value', sa.Numeric(18, 4), nullable=False),
    sa.Column('rate_currency', sa.Enum('USD', 'CUP', 'EUR', 'MLC', 'MXN', name='vipcurrency', create_constraint=True), nullable=False),
    sa.Column('give_method', sa.Enum('CASH', 'TRANSFER', 'ZELLE', 'MLC_BALANCE', 'OTHER', name='vipsettlementmethod', create_constraint=True), nullable=False),
    sa.Column('give_method_detail', sa.String(), nullable=True),
    sa.Column('give_location', sa.String(), nullable=False),
    sa.Column('receive_method', sa.Enum('CASH', 'TRANSFER', 'ZELLE', 'MLC_BALANCE', 'OTHER', name='vipsettlementmethod', create_constraint=True), nullable=False),
    sa.Column('receive_method_detail', sa.String(), nullable=True),
    sa.Column('receive_location', sa.String(), nullable=False),
    sa.Column('duration_hours', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'ACTIVE', 'CLOSED', 'EXPIRED', name='vipofferstatus', create_constraint=True), nullable=False),
    sa.Column('source_message_id', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['channel_identity_id'], ['channel_identities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_message_id', name='uq_vip_offer_source_message')
    )
    op.create_index(op.f('ix_whatsapp_vip_offers_id'), 'whatsapp_vip_offers', ['id'], unique=False)
    op.create_index(op.f('ix_whatsapp_vip_offers_channel_identity_id'), 'whatsapp_vip_offers', ['channel_identity_id'], unique=False)
    op.create_index(op.f('ix_whatsapp_vip_offers_status'), 'whatsapp_vip_offers', ['status'], unique=False)
    op.create_index(op.f('ix_whatsapp_vip_offers_expires_at'), 'whatsapp_vip_offers', ['expires_at'], unique=False)

    op.create_table('whatsapp_vip_offer_interests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('offer_id', sa.Integer(), nullable=False),
    sa.Column('interested_channel_identity_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['interested_channel_identity_id'], ['channel_identities.id'], ),
    sa.ForeignKeyConstraint(['offer_id'], ['whatsapp_vip_offers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('offer_id', 'interested_channel_identity_id', name='uq_vip_offer_interest')
    )
    op.create_index(op.f('ix_whatsapp_vip_offer_interests_id'), 'whatsapp_vip_offer_interests', ['id'], unique=False)
    op.create_index(op.f('ix_whatsapp_vip_offer_interests_offer_id'), 'whatsapp_vip_offer_interests', ['offer_id'], unique=False)
    op.create_index(op.f('ix_whatsapp_vip_offer_interests_interested_channel_identity_id'), 'whatsapp_vip_offer_interests', ['interested_channel_identity_id'], unique=False)

    op.create_table('whatsapp_outbound_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('whatsapp_id', sa.String(), nullable=False),
    sa.Column('body_text', sa.String(), nullable=False),
    sa.Column(
        'status',
        sa.Enum('PENDING', 'SENDING', 'META_ACCEPTED', 'DELIVERED', 'FAILED_RETRYABLE', 'FAILED_TERMINAL', 'UNCERTAIN_NEEDS_RECONCILIATION', name='outboundmessagestatus', create_constraint=True),
        nullable=False,
    ),
    sa.Column('wamid', sa.String(), nullable=True),
    sa.Column('attempt_count', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.String(), nullable=True),
    sa.Column('source_message_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('wamid')
    )
    op.create_index(op.f('ix_whatsapp_outbound_messages_id'), 'whatsapp_outbound_messages', ['id'], unique=False)
    op.create_index(op.f('ix_whatsapp_outbound_messages_whatsapp_id'), 'whatsapp_outbound_messages', ['whatsapp_id'], unique=False)
    op.create_index(op.f('ix_whatsapp_outbound_messages_status'), 'whatsapp_outbound_messages', ['status'], unique=False)
    op.create_index(op.f('ix_whatsapp_outbound_messages_wamid'), 'whatsapp_outbound_messages', ['wamid'], unique=False)
    op.create_index(op.f('ix_whatsapp_outbound_messages_source_message_id'), 'whatsapp_outbound_messages', ['source_message_id'], unique=False)
    op.create_index(op.f('ix_whatsapp_outbound_messages_next_attempt_at'), 'whatsapp_outbound_messages', ['next_attempt_at'], unique=False)

    op.create_table('whatsapp_outbox_worker_lease',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('owner_token', sa.String(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('whatsapp_outbox_worker_lease')

    op.drop_index(op.f('ix_whatsapp_outbound_messages_next_attempt_at'), table_name='whatsapp_outbound_messages')
    op.drop_index(op.f('ix_whatsapp_outbound_messages_source_message_id'), table_name='whatsapp_outbound_messages')
    op.drop_index(op.f('ix_whatsapp_outbound_messages_wamid'), table_name='whatsapp_outbound_messages')
    op.drop_index(op.f('ix_whatsapp_outbound_messages_status'), table_name='whatsapp_outbound_messages')
    op.drop_index(op.f('ix_whatsapp_outbound_messages_whatsapp_id'), table_name='whatsapp_outbound_messages')
    op.drop_index(op.f('ix_whatsapp_outbound_messages_id'), table_name='whatsapp_outbound_messages')
    op.drop_table('whatsapp_outbound_messages')

    op.drop_index(op.f('ix_whatsapp_vip_offer_interests_interested_channel_identity_id'), table_name='whatsapp_vip_offer_interests')
    op.drop_index(op.f('ix_whatsapp_vip_offer_interests_offer_id'), table_name='whatsapp_vip_offer_interests')
    op.drop_index(op.f('ix_whatsapp_vip_offer_interests_id'), table_name='whatsapp_vip_offer_interests')
    op.drop_table('whatsapp_vip_offer_interests')

    op.drop_index(op.f('ix_whatsapp_vip_offers_expires_at'), table_name='whatsapp_vip_offers')
    op.drop_index(op.f('ix_whatsapp_vip_offers_status'), table_name='whatsapp_vip_offers')
    op.drop_index(op.f('ix_whatsapp_vip_offers_channel_identity_id'), table_name='whatsapp_vip_offers')
    op.drop_index(op.f('ix_whatsapp_vip_offers_id'), table_name='whatsapp_vip_offers')
    op.drop_table('whatsapp_vip_offers')

    op.drop_index(op.f('ix_whatsapp_vip_sessions_id'), table_name='whatsapp_vip_sessions')
    op.drop_table('whatsapp_vip_sessions')

    op.drop_index(op.f('ix_whatsapp_vip_memberships_channel_identity_id'), table_name='whatsapp_vip_memberships')
    op.drop_index(op.f('ix_whatsapp_vip_memberships_id'), table_name='whatsapp_vip_memberships')
    op.drop_table('whatsapp_vip_memberships')

    op.drop_index(op.f('ix_whatsapp_vip_invites_code_hash'), table_name='whatsapp_vip_invites')
    op.drop_index(op.f('ix_whatsapp_vip_invites_id'), table_name='whatsapp_vip_invites')
    op.drop_table('whatsapp_vip_invites')

    _OLD_ACTIONS = (
        'AUTH_FAILURE', 'AUTH_SUCCESS', 'INVITE_CREATED', 'INVITE_REDEEMED', 'INVITE_REDEMPTION_DENIED',
        'INVITE_REVOKED', 'MEMBERSHIP_SUSPENDED', 'MEMBERSHIP_REACTIVATED', 'ROLE_CHANGED',
        'SETTLEMENT_DETAIL_VIEWED', 'SETTLEMENT_DETAIL_CREATED', 'SETTLEMENT_DETAIL_REVOKED',
        'DISPUTE_EVIDENCE_VIEWED', 'DISPUTE_EVIDENCE_SUBMITTED', 'DISPUTE_ASSIGNMENT_CREATED',
        'DISPUTE_ASSIGNMENT_REVOKED', 'REPORT_FILED', 'REPORT_REVIEWED', 'RATE_LIMIT_TRIGGERED',
        'ADMIN_BOOTSTRAPPED', 'WHATSAPP_ACCOUNT_LINKED', 'TELEGRAM_IDENTITY_REGISTERED',
        'TELEGRAM_MEMBERSHIP_SUSPENDED', 'TELEGRAM_MEMBERSHIP_REACTIVATED', 'TELEGRAM_OFFER_CREATED',
        'TELEGRAM_OFFER_MATCHED', 'TELEGRAM_OFFER_STATUS_CHANGED',
    )
    _VIP_ACTIONS = (
        'VIP_WHATSAPP_ID_AUTHORIZED', 'VIP_INVITE_CREATED', 'VIP_INVITE_REDEEMED', 'VIP_MEMBERSHIP_REVOKED',
        'VIP_OFFER_CREATED', 'VIP_OFFER_CLOSED', 'VIP_OFFER_INTEREST_REGISTERED',
        'VIP_OUTBOUND_DELIVERY_UNCERTAIN', 'VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL',
    )
    _NEW_ACTIONS = _OLD_ACTIONS + _VIP_ACTIONS

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with op.batch_alter_table('security_events') as batch_op:
            batch_op.alter_column(
                'action',
                existing_type=sa.Enum(*_NEW_ACTIONS, name='securityeventtype', create_constraint=True),
                type_=sa.Enum(*_OLD_ACTIONS, name='securityeventtype', create_constraint=True),
            )
        with op.batch_alter_table('channel_identities') as batch_op:
            batch_op.alter_column(
                'channel',
                existing_type=sa.Enum('TELEGRAM', 'WHATSAPP', name='channel', create_constraint=True),
                type_=sa.Enum('TELEGRAM', name='channel', create_constraint=True),
            )
    # else: same documented PostgreSQL limitation as prior migrations -- no
    # ALTER TYPE ... DROP VALUE; the extra enum values stay inert once no row
    # (and now no table) references them.

    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS outboundmessagestatus")
        op.execute("DROP TYPE IF EXISTS vipofferstatus")
        op.execute("DROP TYPE IF EXISTS vipsettlementmethod")
        op.execute("DROP TYPE IF EXISTS vipcurrency")
        op.execute("DROP TYPE IF EXISTS vipofferintent")
        op.execute("DROP TYPE IF EXISTS vipwizardstate")
        op.execute("DROP TYPE IF EXISTS vipauthorizationmethod")
