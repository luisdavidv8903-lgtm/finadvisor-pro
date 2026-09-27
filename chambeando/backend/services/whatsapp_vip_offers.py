"""
Transactional logic for WhatsApp VIP offer-board announcements -- separated
from whatsapp_vip_router.py for the same reason services/telegram_offers.py
is separated from telegram_router.py (testable without a fake WhatsApp
webhook payload).

Every state-changing function requires an ACTIVE WhatsAppVipMembershipDB
(via whatsapp_vip_identity.require_active_vip) before doing anything.

No P2POrderDB, no wallet, no escrow reference anywhere in this module.

Revision 2026-09-27 (audit): every function here (create_offer, close_offer,
register_interest, and the lazy-expiry flip in _lazily_expire_if_needed) is
reachable ONLY from the VIP message path (whatsapp_vip_router.py) -- never
from vip_admin.py or any other standalone entry point -- so NONE of them
commit anymore (db.flush() where a subsequent read/refresh in the SAME
function needs to see the pending change; nothing beyond that). Every
mutation lands in the caller's single atomic commit
(_handle_vip_message_atomically in routers/whatsapp.py), together with that
same message's wizard/session state, outbox reply, and webhook idempotency
marker. Each function's own idempotency guard (source_message_id's unique
constraint for create_offer, the (offer_id, interested_channel_identity_id)
unique constraint for register_interest, the CLOSED-is-a-no-op check for
close_offer) is what makes it safe for a redelivered message to re-enter
here after a rollback -- see each docstring below.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from .. import timeutils
from ..messaging.whatsapp_vip_identity import require_active_vip
from ..models import (
    Channel,
    ChannelIdentityDB,
    SecurityEventType,
    VipCurrency,
    VipOfferIntent,
    VipOfferStatus,
    VipSettlementMethod,
    WhatsAppVipOfferDB,
    WhatsAppVipOfferInterestDB,
)
from ..security.audit import log_security_event

# V1: fixed rate only. Percentage-based offers appear in the source chat
# evidence (CHAMBEANDO_VIP_OFERTAS_V1.md, "CUP digital frente a efectivo...
# al 10%") but its calculation/presentation is not yet unambiguous enough to
# publish -- deferred until that is defined, per explicit instruction. This
# module has no percentage fields/paths at all (not just "hidden" -- absent).

# 24h default, 3 days optional -- the only two allowed durations in V1.
ALLOWED_DURATION_HOURS = (24, 72)
DEFAULT_DURATION_HOURS = 24


class OfferError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _effective_status(offer: WhatsAppVipOfferDB, now) -> VipOfferStatus:
    """Computed at READ time, never trusted purely from the stored column --
    an ACTIVE offer whose expires_at has passed is EXPIRED right now, even if
    no sweep has ever run to flip the stored value (there is no scheduled
    sweep for this in V1 -- listing/lookup always self-corrects instead)."""
    if offer.status == VipOfferStatus.ACTIVE and timeutils.ensure_utc(offer.expires_at) <= now:
        return VipOfferStatus.EXPIRED
    return offer.status


def _lazily_expire_if_needed(db: Session, offer: WhatsAppVipOfferDB, now) -> WhatsAppVipOfferDB:
    """Opportunistically persists the ACTIVE -> EXPIRED flip the first time
    any code path notices it, so MIS OFERTAS shows an accurate stored status
    too, not just filtered-out-of-OFERTAS. Safe to call repeatedly (no-op
    once already EXPIRED) and safe under concurrent callers (a redundant
    UPDATE to the same value is harmless, no lost-update risk for this
    one-way transition)."""
    if offer.status == VipOfferStatus.ACTIVE and timeutils.ensure_utc(offer.expires_at) <= now:
        offer.status = VipOfferStatus.EXPIRED
        offer.updated_at = now
        db.flush()
        db.refresh(offer)
    return offer


def create_offer(
    db: Session,
    identity: ChannelIdentityDB,
    *,
    source_message_id: str,
    intent: VipOfferIntent,
    give_currency: VipCurrency,
    receive_currency: VipCurrency,
    amount: Decimal,
    min_amount: Decimal | None,
    rate_value: Decimal,
    rate_currency: VipCurrency,
    give_method: VipSettlementMethod,
    give_method_detail: str | None,
    give_location: str,
    receive_method: VipSettlementMethod,
    receive_method_detail: str | None,
    receive_location: str,
    duration_hours: int,
) -> WhatsAppVipOfferDB:
    """Idempotent by source_message_id: if the inbound CONFIRMAR that
    triggered this exact call was already processed (webhook redelivery
    after a crash between commit and ack -- see routers/whatsapp.py), this
    returns the ALREADY-CREATED offer instead of inserting a duplicate. The
    unique constraint on source_message_id is the real guarantee; this
    pre-check just avoids relying on catching IntegrityError at every call
    site."""
    require_active_vip(db, identity)

    existing = db.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.source_message_id == source_message_id).first()
    if existing is not None:
        return existing

    if give_currency == receive_currency:
        raise OfferError("give and receive currency must differ")
    if rate_currency not in (give_currency, receive_currency):
        raise OfferError("rate currency must be either the give or receive currency")
    if duration_hours not in ALLOWED_DURATION_HOURS:
        raise OfferError("invalid duration")
    if give_method == VipSettlementMethod.OTHER and not (give_method_detail or "").strip():
        raise OfferError("give method detail required for OTHER")
    if receive_method == VipSettlementMethod.OTHER and not (receive_method_detail or "").strip():
        raise OfferError("receive method detail required for OTHER")

    now = timeutils.utcnow()
    offer = WhatsAppVipOfferDB(
        channel_identity_id=identity.id,
        intent=intent,
        give_currency=give_currency,
        receive_currency=receive_currency,
        amount=amount,
        min_amount=min_amount,
        rate_value=rate_value,
        rate_currency=rate_currency,
        give_method=give_method,
        give_method_detail=(give_method_detail or "").strip() or None,
        give_location=give_location.strip(),
        receive_method=receive_method,
        receive_method_detail=(receive_method_detail or "").strip() or None,
        receive_location=receive_location.strip(),
        duration_hours=duration_hours,
        status=VipOfferStatus.ACTIVE,
        source_message_id=source_message_id,
        expires_at=now + timedelta(hours=duration_hours),
    )
    db.add(offer)
    db.flush()
    log_security_event(db, action=SecurityEventType.VIP_OFFER_CREATED, actor_user_id=None, target_type="whatsapp_vip_offer", target_id=offer.id)
    db.refresh(offer)
    return offer


def list_active_offers(db: Session, *, owner_channel: Channel, exclude_identity: ChannelIdentityDB | None = None, limit: int = 10) -> list[WhatsAppVipOfferDB]:
    """OFERTAS -- never returns an offer whose expires_at has passed, even if
    its stored status column still says ACTIVE (see _effective_status). This
    is a read-time filter on expires_at directly, not a trust-the-column
    query -- correct regardless of whether any lazy-expire write has run.

    `owner_channel` (2026-09-27, cross-channel isolation audit, second
    pass): REQUIRED, no default -- this function had NO channel filter at
    all until this fix, so OFERTAS on Telegram showed WhatsApp-owned offers
    and vice versa (WhatsAppVipOfferDB is the shared table both routers
    write to; see get_offer_by_id's own owner_channel filter, added in the
    first pass, for the same reasoning applied there). Made required rather
    than optional specifically so a future caller can never silently
    reintroduce the leak by forgetting to pass it -- omitting it is a
    TypeError, not a quiet cross-channel listing. get_my_offers below needs
    no equivalent parameter: it is already pinned to one exact
    channel_identity_id, which can only ever belong to a single channel."""
    now = timeutils.utcnow()
    query = (
        db.query(WhatsAppVipOfferDB)
        .join(ChannelIdentityDB, WhatsAppVipOfferDB.channel_identity_id == ChannelIdentityDB.id)
        .filter(WhatsAppVipOfferDB.status == VipOfferStatus.ACTIVE, WhatsAppVipOfferDB.expires_at > now, ChannelIdentityDB.channel == owner_channel)
    )
    if exclude_identity is not None:
        query = query.filter(WhatsAppVipOfferDB.channel_identity_id != exclude_identity.id)
    return query.order_by(WhatsAppVipOfferDB.id.desc()).limit(limit).all()


def get_my_offers(db: Session, identity: ChannelIdentityDB, *, limit: int = 10) -> list[WhatsAppVipOfferDB]:
    """MIS OFERTAS -- includes closed/expired (for the owner's own history),
    with the stored status lazily corrected to EXPIRED on the way out so what
    the owner sees always matches reality."""
    now = timeutils.utcnow()
    offers = (
        db.query(WhatsAppVipOfferDB)
        .filter(WhatsAppVipOfferDB.channel_identity_id == identity.id)
        .order_by(WhatsAppVipOfferDB.id.desc())
        .limit(limit)
        .all()
    )
    return [_lazily_expire_if_needed(db, o, now) for o in offers]


def get_offer_by_id(db: Session, offer_id: int, *, owner_channel: Channel | None = None) -> WhatsAppVipOfferDB | None:
    """`owner_channel` (2026-09-27, cross-channel isolation audit): when
    given, an offer whose OWNER's ChannelIdentityDB.channel does not match
    is treated as not found -- same as a nonexistent id, never a distinct
    error. Both whatsapp_vip_router.py and telegram_vip_router.py now pass
    their own channel here for every lookup that can lead to an action
    (viewing via deep link, ME INTERESA, CERRAR): WhatsAppVipOfferDB and
    WhatsAppVipOfferInterestDB are shared by BOTH routers (same tables,
    channel-agnostic FK), so without this check a Telegram identity could
    act on a WhatsApp-owned offer (or vice versa) purely by guessing/typing
    its numeric id -- and register_interest's owner-notification lookup
    would then try to reach that owner through the WRONG transport (a
    WhatsApp phone-derived id fed to the Telegram Bot API, or a Telegram
    numeric id fed to Meta's Cloud API). `None` (the default) keeps every
    existing caller that doesn't lead to a cross-channel send (e.g. any
    future channel-neutral admin/reporting use) unaffected."""
    offer = db.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.id == offer_id).first()
    if offer is None:
        return None
    if owner_channel is not None:
        owner_identity = db.query(ChannelIdentityDB).filter(ChannelIdentityDB.id == offer.channel_identity_id).first()
        if owner_identity is None or owner_identity.channel != owner_channel:
            return None
    return _lazily_expire_if_needed(db, offer, timeutils.utcnow())


def close_offer(db: Session, identity: ChannelIdentityDB, offer: WhatsAppVipOfferDB) -> WhatsAppVipOfferDB:
    """CERRAR <id> -- owner only. Idempotent: closing an already-CLOSED offer
    is a calm no-op (returns it unchanged), never an error -- a redelivered
    CERRAR must never surface a confusing failure for an action that already
    succeeded."""
    require_active_vip(db, identity)
    if offer.channel_identity_id != identity.id:
        raise OfferError("not your offer")

    effective = _effective_status(offer, timeutils.utcnow())
    if effective == VipOfferStatus.CLOSED:
        return offer
    if effective != VipOfferStatus.ACTIVE:
        raise OfferError(f"cannot close an offer in status {effective.value}")

    offer.status = VipOfferStatus.CLOSED
    offer.closed_at = timeutils.utcnow()
    offer.updated_at = offer.closed_at
    log_security_event(db, action=SecurityEventType.VIP_OFFER_CLOSED, actor_user_id=None, target_type="whatsapp_vip_offer", target_id=offer.id)
    db.flush()
    db.refresh(offer)
    return offer


def register_interest(db: Session, identity: ChannelIdentityDB, offer: WhatsAppVipOfferDB) -> tuple[WhatsAppVipOfferInterestDB, bool]:
    """ME INTERESA <id> -- returns (row, created). DB-enforced idempotency
    via the unique constraint on (offer_id, interested_channel_identity_id):
    a second 'ME INTERESA' from the same person on the same offer is a no-op
    that returns the EXISTING row with created=False, never a duplicate
    notification storm to the owner."""
    require_active_vip(db, identity)

    effective = _effective_status(offer, timeutils.utcnow())
    if effective != VipOfferStatus.ACTIVE:
        raise OfferError(f"offer is {effective.value}, not open for interest")
    if offer.channel_identity_id == identity.id:
        raise OfferError("cannot express interest in your own offer")

    existing = (
        db.query(WhatsAppVipOfferInterestDB)
        .filter(WhatsAppVipOfferInterestDB.offer_id == offer.id, WhatsAppVipOfferInterestDB.interested_channel_identity_id == identity.id)
        .first()
    )
    if existing is not None:
        return existing, False

    interest = WhatsAppVipOfferInterestDB(offer_id=offer.id, interested_channel_identity_id=identity.id)
    db.add(interest)
    db.flush()
    log_security_event(db, action=SecurityEventType.VIP_OFFER_INTEREST_REGISTERED, actor_user_id=None, target_type="whatsapp_vip_offer", target_id=offer.id)
    db.refresh(interest)
    return interest, True
