"""
Telegram identity/membership -- the channel-agnostic counterpart of
messaging/identity.py, but without wallet proof: a Telegram identity becomes
an ACTIVE member the moment it registers, because the private Telegram group
itself (invite-only, managed outside this codebase) is the access gate for
V1 -- there is no separate invite-code system here on purpose (YAGNI; see
TELEGRAM_PILOT.md). `user_id` on ChannelIdentityDB stays NULL; linking to a
wallet-proven UserDB is a future capability, not something V1 does.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import timeutils
from ..models import (
    Channel,
    ChannelIdentityDB,
    MemberRole,
    MembershipStatus,
    SecurityEventType,
    TelegramMembershipDB,
)
from ..security.audit import log_security_event


def get_or_create_identity(db: Session, channel_user_id: str) -> ChannelIdentityDB:
    identity = (
        db.query(ChannelIdentityDB)
        .filter(ChannelIdentityDB.channel == Channel.TELEGRAM, ChannelIdentityDB.channel_user_id == channel_user_id)
        .first()
    )
    if identity is not None:
        return identity
    identity = ChannelIdentityDB(channel=Channel.TELEGRAM, channel_user_id=channel_user_id)
    db.add(identity)
    db.commit()
    db.refresh(identity)
    return identity


def get_membership(db: Session, identity: ChannelIdentityDB) -> TelegramMembershipDB | None:
    return db.query(TelegramMembershipDB).filter(TelegramMembershipDB.channel_identity_id == identity.id).first()


def register_or_get_membership(db: Session, identity: ChannelIdentityDB) -> TelegramMembershipDB:
    """First contact -> ACTIVE MEMBER membership. Idempotent: calling this
    again for an already-registered identity just returns the existing row
    (never resets a SUSPENDED membership back to ACTIVE -- see reactivate())."""
    membership = get_membership(db, identity)
    if membership is not None:
        return membership

    membership = TelegramMembershipDB(channel_identity_id=identity.id)
    db.add(membership)
    log_security_event(
        db,
        action=SecurityEventType.TELEGRAM_IDENTITY_REGISTERED,
        actor_user_id=None,
        target_type="channel_identity",
        target_id=identity.id,
    )
    db.commit()
    db.refresh(membership)
    return membership


class MembershipRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def require_active_membership(db: Session, identity: ChannelIdentityDB) -> TelegramMembershipDB:
    """Fail-closed gate, same spirit as deps.get_current_membership: no
    membership or a non-ACTIVE one is always rejected, never let through
    'just in case'. Callers (router, offer/matching services) must call this
    before offer creation, matching, or any trade-state change."""
    membership = get_membership(db, identity)
    if membership is None:
        raise MembershipRejected("not registered")
    if membership.status != MembershipStatus.ACTIVE:
        raise MembershipRejected("suspended")
    return membership


def suspend_membership(db: Session, target: TelegramMembershipDB, *, by_identity: ChannelIdentityDB, reason: str) -> TelegramMembershipDB:
    target.status = MembershipStatus.SUSPENDED
    target.suspended_by_channel_identity_id = by_identity.id
    target.suspended_at = timeutils.utcnow()
    target.suspension_reason = reason
    log_security_event(
        db,
        action=SecurityEventType.TELEGRAM_MEMBERSHIP_SUSPENDED,
        actor_user_id=None,
        target_type="telegram_membership",
        target_id=target.id,
        reason=reason,
    )
    db.commit()
    db.refresh(target)
    return target


def reactivate_membership(db: Session, target: TelegramMembershipDB, *, by_identity: ChannelIdentityDB) -> TelegramMembershipDB:
    target.status = MembershipStatus.ACTIVE
    target.suspended_by_channel_identity_id = None
    target.suspended_at = None
    target.suspension_reason = None
    log_security_event(
        db,
        action=SecurityEventType.TELEGRAM_MEMBERSHIP_REACTIVATED,
        actor_user_id=None,
        target_type="telegram_membership",
        target_id=target.id,
        reason=f"reactivated by channel_identity_id={by_identity.id}",
    )
    db.commit()
    db.refresh(target)
    return target
