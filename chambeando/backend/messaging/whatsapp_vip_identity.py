"""
WhatsApp VIP pilot identity/membership -- the channel-agnostic counterpart of
telegram_identity.py, but with a DELIBERATELY different access rule: talking
to the bot NEVER grants access by itself (there is no private, invite-only
WhatsApp group gating this the way there is for Telegram -- a webhook message
proves nothing about group membership). The ONLY ways to obtain an ACTIVE
WhatsAppVipMembershipDB row are:

  1. authorize_whatsapp_id() -- called by vip_admin.py, a LOCAL operator-run
     script, never reachable from chat/HTTP.
  2. redeem_vip_invite() -- redeeming a single-use WhatsAppVipInviteDB code
     the operator generated and shared out of band.

No other function in this module ever creates a WhatsAppVipMembershipDB row.

Revision 2026-09-27 (audit): commit discipline split explicitly by who owns
the unit of work. stage_identity() never commits (db.flush() only) -- it is
the shared core both sides build on. authorize_whatsapp_id/create_vip_invite/
revoke_vip_invite/revoke_membership are vip_admin.py's OWN standalone units
of work (one operator-run CLI invocation, one session, one commit each) and
keep committing, now via stage_identity so a brand-new identity and
whatever else that admin action does land in that SAME single commit
instead of two. redeem_vip_invite() is reachable ONLY from the VIP message
path (whatsapp_vip_router.py's `VIP <code>` handler) and now NEVER commits
-- its mutations (staged identity, invite.used_count, the new membership)
are the caller's responsibility to commit, together with that same
message's wizard/session state, outbox reply, and webhook idempotency
marker, as ONE atomic transaction (see routers/whatsapp.py's
_handle_vip_message_atomically and whatsapp_vip_router.py's module
docstring). get_or_create_identity() remains as a committing convenience
wrapper around stage_identity for callers outside both of those units
(tests, any future standalone script) -- the message path never calls it.

Revision 2026-09-27 (Telegram-owned VIP offer pilot): stage_identity(),
get_or_create_identity(), and redeem_vip_invite() now take an explicit
`channel: Channel = Channel.WHATSAPP` parameter instead of hardcoding
Channel.WHATSAPP. This is the ONLY change needed to let a VIP offer have a
Telegram owner: WhatsAppVipOfferDB/WhatsAppVipMembershipDB/
WhatsAppVipInviteDB/WhatsAppVipSessionDB were already keyed off
ChannelIdentityDB.channel_identity_id, which was already channel-agnostic
in shape -- only this identity-lookup layer was hardcoded to WhatsApp. The
default keeps every existing WhatsApp caller (whatsapp_vip_router.py,
vip_admin.py) byte-for-byte unchanged; messaging/telegram_vip_router.py is
the only caller that ever passes channel=Channel.TELEGRAM. This module's
own tables and their naming ("whatsapp_vip_*") are unchanged on purpose --
renaming them was judged a larger, riskier migration than this pilot's
scope, and out of proportion to a schema that was already correct.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import timeutils
from ..models import (
    Channel,
    ChannelIdentityDB,
    MembershipStatus,
    SecurityEventType,
    VipAuthorizationMethod,
    WhatsAppVipInviteDB,
    WhatsAppVipMembershipDB,
)
from ..security.audit import log_security_event


def stage_identity(db: Session, whatsapp_id: str, channel: Channel = Channel.WHATSAPP) -> ChannelIdentityDB:
    """Get-or-create WITHOUT committing -- db.flush() only, so a NEW identity
    row lands in whichever atomic unit the caller is building (the VIP
    message path's single commit in routers/whatsapp.py or
    routers/telegram.py, or an admin function's own single commit below).
    Never touches an EXISTING identity at all (pure SELECT), so this never
    risks re-flushing unrelated pending changes on a row that was already
    there.

    `channel` defaults to WHATSAPP so every pre-existing caller is
    unaffected; messaging/telegram_vip_router.py is the only caller that
    passes channel=Channel.TELEGRAM (see this module's revision note). The
    lookup/creation reuses the SAME (channel, channel_user_id)-keyed
    ChannelIdentityDB row telegram_identity.py's own get_or_create_identity
    would find for that Telegram user -- never a second, duplicate identity
    row for the same real person."""
    identity = (
        db.query(ChannelIdentityDB)
        .filter(ChannelIdentityDB.channel == channel, ChannelIdentityDB.channel_user_id == whatsapp_id)
        .first()
    )
    if identity is not None:
        return identity
    identity = ChannelIdentityDB(channel=channel, channel_user_id=whatsapp_id)
    db.add(identity)
    db.flush()
    return identity


def get_or_create_identity(db: Session, whatsapp_id: str, channel: Channel = Channel.WHATSAPP) -> ChannelIdentityDB:
    """Standalone, committing wrapper around stage_identity -- for callers
    OUTSIDE the VIP message atomic unit (tests exercising this module in
    isolation, or any future direct caller). Never used by the message path
    itself, which calls stage_identity directly (see
    whatsapp_vip_router.py/telegram_vip_router.py) so identity creation is
    never its own separate commit."""
    identity = stage_identity(db, whatsapp_id, channel)
    db.commit()
    db.refresh(identity)
    return identity


def get_membership(db: Session, identity: ChannelIdentityDB) -> WhatsAppVipMembershipDB | None:
    return db.query(WhatsAppVipMembershipDB).filter(WhatsAppVipMembershipDB.channel_identity_id == identity.id).first()


class MembershipRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def require_active_vip(db: Session, identity: ChannelIdentityDB) -> WhatsAppVipMembershipDB:
    """Fail-closed gate every VIP command must call before doing anything --
    same spirit as telegram_identity.require_active_membership, but note the
    different failure meaning here: 'not registered' means 'never
    authorized', not 'hasn't said /start yet'."""
    membership = get_membership(db, identity)
    if membership is None:
        raise MembershipRejected("not authorized")
    if membership.status != MembershipStatus.ACTIVE:
        raise MembershipRejected("suspended")
    return membership


# ---------------------------------------------------------------------------
# Operator-only (vip_admin.py). Never called from routers/whatsapp.py or any
# HTTP/chat-reachable path.
# ---------------------------------------------------------------------------


def authorize_whatsapp_id(db: Session, whatsapp_id: str, *, note: str | None = None) -> WhatsAppVipMembershipDB:
    """Directly grant VIP access to a known WhatsApp id. Idempotent: calling
    this again for an already-active identity is a no-op that returns the
    existing row (never resets a SUSPENDED membership -- use reactivate
    explicitly for that, mirroring telegram_identity's same rule).

    Admin's own explicit unit of work (separate from, and never shared
    with, the VIP message path's atomic unit): builds on stage_identity
    (no commit of its own) so a brand-new identity and the membership it's
    granted land in ONE commit here, instead of the two separate commits
    this used to be."""
    identity = stage_identity(db, whatsapp_id)
    existing = get_membership(db, identity)
    if existing is not None:
        return existing

    membership = WhatsAppVipMembershipDB(channel_identity_id=identity.id, authorized_via=VipAuthorizationMethod.DIRECT)
    db.add(membership)
    log_security_event(
        db,
        action=SecurityEventType.VIP_WHATSAPP_ID_AUTHORIZED,
        actor_user_id=None,
        target_type="channel_identity",
        target_id=identity.id,
        reason=note,
    )
    db.commit()
    db.refresh(membership)
    return membership


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_vip_invite(db: Session, *, max_uses: int = 1, ttl_days: int = 14, note: str | None = None) -> str:
    """Returns the PLAINTEXT code exactly once -- only code_hash is ever
    persisted (same discipline as InviteDB.code_hash)."""
    code = secrets.token_urlsafe(12)
    invite = WhatsAppVipInviteDB(
        code_hash=_hash_code(code),
        max_uses=max_uses,
        expires_at=timeutils.utcnow() + timedelta(days=ttl_days),
        note=note,
    )
    db.add(invite)
    db.flush()
    log_security_event(db, action=SecurityEventType.VIP_INVITE_CREATED, actor_user_id=None, target_type="whatsapp_vip_invite", target_id=invite.id, reason=note)
    db.commit()
    return code


def revoke_vip_invite(db: Session, invite: WhatsAppVipInviteDB) -> WhatsAppVipInviteDB:
    invite.revoked_at = timeutils.utcnow()
    db.commit()
    db.refresh(invite)
    return invite


class InviteRedemptionError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def redeem_vip_invite(db: Session, whatsapp_id: str, code: str, channel: Channel = Channel.WHATSAPP) -> WhatsAppVipMembershipDB:
    """Chat-reachable (VIP <code>) -- but only grants access to whoever
    already knows a valid code, which only the operator hands out. Single-use
    by default (max_uses=1); an already-authorized identity redeeming a
    (different) code again is rejected -- one membership per identity,
    enforced by the same unique constraint authorize_whatsapp_id relies on.

    `channel` (default WHATSAPP) picks which ChannelIdentityDB this code is
    being redeemed against -- see stage_identity's revision note. The invite
    code itself is NOT channel-scoped (an operator-generated code works the
    same whether redeemed from WhatsApp or Telegram); only the identity it
    grants membership to is channel-specific.

    Never commits -- only ever reachable from the VIP message path
    (whatsapp_vip_router.py's `VIP <code>` handler), so every mutation here
    (identity creation via stage_identity, the invite reservation, the new
    membership) lands in the caller's single atomic commit
    (_handle_vip_message_atomically in routers/whatsapp.py), together with
    that same message's outbox reply and idempotency marker.

    Revision 2026-09-27 (audit finding): the actual reservation of a use is
    an ATOMIC compare-and-swap UPDATE (same pattern as acquire_lease/
    attempt_send_one), never a Python-level "read used_count, check it,
    then write used_count + 1" -- that read-then-write let two concurrent
    redemptions of the SAME max_uses=1 code both read used_count=0, both
    pass the check, and both create a membership before either's write
    landed. The UPDATE's WHERE clause re-checks not-revoked, not-expired,
    and used_count < max_uses in the SAME statement the database executes;
    whoever's UPDATE affects zero rows lost the race and creates NO
    membership -- no exception in this module can ever run between the
    check and the write again, because there IS no such gap anymore."""
    identity = stage_identity(db, whatsapp_id, channel)
    if get_membership(db, identity) is not None:
        raise InviteRedemptionError("already authorized")

    code_hash = _hash_code(code.strip())
    invite = db.query(WhatsAppVipInviteDB).filter(WhatsAppVipInviteDB.code_hash == code_hash).first()
    if invite is None:
        raise InviteRedemptionError("invalid code")
    if invite.revoked_at is not None:
        raise InviteRedemptionError("revoked")
    if timeutils.ensure_utc(invite.expires_at) < timeutils.utcnow():
        raise InviteRedemptionError("expired")
    if invite.used_count >= invite.max_uses:
        raise InviteRedemptionError("exhausted")

    now = timeutils.utcnow()
    reservation = db.execute(
        update(WhatsAppVipInviteDB)
        .where(WhatsAppVipInviteDB.id == invite.id)
        .where(WhatsAppVipInviteDB.revoked_at.is_(None))
        .where(WhatsAppVipInviteDB.expires_at > now)
        .where(WhatsAppVipInviteDB.used_count < WhatsAppVipInviteDB.max_uses)
        .values(used_count=WhatsAppVipInviteDB.used_count + 1)
        .execution_options(synchronize_session=False)
    )
    if reservation.rowcount != 1:
        # Lost the race to a concurrent redemption (or someone revoked/it
        # expired in the exact same instant) -- re-read to report the
        # accurate reason. NO membership is created on this path.
        db.refresh(invite)
        if invite.revoked_at is not None:
            raise InviteRedemptionError("revoked")
        if timeutils.ensure_utc(invite.expires_at) < timeutils.utcnow():
            raise InviteRedemptionError("expired")
        raise InviteRedemptionError("exhausted")
    db.refresh(invite)

    membership = WhatsAppVipMembershipDB(channel_identity_id=identity.id, authorized_via=VipAuthorizationMethod.INVITE, invite_id=invite.id)
    db.add(membership)
    log_security_event(
        db,
        action=SecurityEventType.VIP_INVITE_REDEEMED,
        actor_user_id=None,
        target_type="whatsapp_vip_invite",
        target_id=invite.id,
    )
    db.flush()
    db.refresh(membership)
    return membership


def revoke_membership(db: Session, membership: WhatsAppVipMembershipDB, *, reason: str) -> WhatsAppVipMembershipDB:
    membership.status = MembershipStatus.SUSPENDED
    membership.revoked_at = timeutils.utcnow()
    membership.revoke_reason = reason
    log_security_event(db, action=SecurityEventType.VIP_MEMBERSHIP_REVOKED, actor_user_id=None, target_type="whatsapp_vip_membership", target_id=membership.id, reason=reason)
    db.commit()
    db.refresh(membership)
    return membership
