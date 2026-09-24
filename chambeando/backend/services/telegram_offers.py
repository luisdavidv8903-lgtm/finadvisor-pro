"""
Transactional logic for Telegram COMPRO/VENDO offers -- separated from the
conversation router the same way services/invites.py is separated from
ConversationRouter, and for the same reason (testable without going through
a fake Telegram update).

Every state-changing function here requires an ACTIVE TelegramMembershipDB
(via telegram_identity.require_active_membership) before doing anything --
a suspended identity can never create, match, or transition an offer.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from .. import timeutils
from ..messaging.telegram_identity import require_active_membership
from ..models import ChannelIdentityDB, OfferSide, OfferStatus, P2POfferDB, SecurityEventType
from ..security.audit import log_security_event


class OfferError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def create_offer(
    db: Session,
    identity: ChannelIdentityDB,
    *,
    side: OfferSide,
    amount: Decimal,
    currency: str,
    rate: Decimal,
    settlement_method: str,
    settlement_location: str,
) -> P2POfferDB:
    require_active_membership(db, identity)

    offer = P2POfferDB(
        channel_identity_id=identity.id,
        side=side,
        amount=amount,
        currency=currency.strip().upper(),
        rate=rate,
        settlement_method=settlement_method.strip(),
        settlement_location=settlement_location.strip(),
    )
    db.add(offer)
    db.flush()
    log_security_event(
        db,
        action=SecurityEventType.TELEGRAM_OFFER_CREATED,
        actor_user_id=None,
        target_type="p2p_offer",
        target_id=offer.id,
    )
    db.commit()
    db.refresh(offer)
    return offer


def list_open_offers(db: Session, *, opposite_of: OfferSide, exclude_identity: ChannelIdentityDB, limit: int = 5) -> list[P2POfferDB]:
    """Only offers of the OPPOSITE side are a real match candidate (someone
    who wants to COMPRO needs a VENDO offer, never another COMPRO)."""
    return (
        db.query(P2POfferDB)
        .filter(
            P2POfferDB.status == OfferStatus.OPEN,
            P2POfferDB.side == opposite_of,
            P2POfferDB.channel_identity_id != exclude_identity.id,
        )
        .order_by(P2POfferDB.id)
        .limit(limit)
        .all()
    )


def list_all_open_offers(db: Session, *, limit: int = 10) -> list[P2POfferDB]:
    """Read-only marketplace browse (both sides) -- no ownership filter, no
    side-effect. Distinct from list_open_offers, which is the MATCH-specific
    opposite-side candidate lookup."""
    return db.query(P2POfferDB).filter(P2POfferDB.status == OfferStatus.OPEN).order_by(P2POfferDB.id.desc()).limit(limit).all()


def match_offer(db: Session, identity: ChannelIdentityDB, own_offer: P2POfferDB, counterparty_offer: P2POfferDB) -> None:
    require_active_membership(db, identity)

    if own_offer.channel_identity_id != identity.id:
        raise OfferError("not your offer")
    if own_offer.status != OfferStatus.OPEN or counterparty_offer.status != OfferStatus.OPEN:
        raise OfferError("one of the offers is no longer open")
    if own_offer.side == counterparty_offer.side:
        raise OfferError("cannot match two offers on the same side")

    now = timeutils.utcnow()
    own_offer.status = OfferStatus.MATCHED
    own_offer.matched_offer_id = counterparty_offer.id
    own_offer.updated_at = now
    counterparty_offer.status = OfferStatus.MATCHED
    counterparty_offer.matched_offer_id = own_offer.id
    counterparty_offer.updated_at = now

    log_security_event(
        db,
        action=SecurityEventType.TELEGRAM_OFFER_MATCHED,
        actor_user_id=None,
        target_type="p2p_offer",
        target_id=own_offer.id,
        reason=f"matched with p2p_offer id={counterparty_offer.id}",
    )
    db.commit()


_ALLOWED_TRANSITIONS: dict[OfferStatus, set[OfferStatus]] = {
    OfferStatus.MATCHED: {OfferStatus.PAYMENT_PENDING, OfferStatus.CANCELLED, OfferStatus.DISPUTED},
    OfferStatus.PAYMENT_PENDING: {OfferStatus.COMPLETED, OfferStatus.CANCELLED, OfferStatus.DISPUTED},
    OfferStatus.OPEN: {OfferStatus.CANCELLED},
}


def allowed_transitions(status: OfferStatus) -> set[OfferStatus]:
    """Read-only lookup for callers (e.g. the Telegram router's action menus)
    that need to know what's offerable WITHOUT duplicating the transition
    table -- the actual enforcement still happens in transition_offer_status."""
    return _ALLOWED_TRANSITIONS.get(status, set())


def transition_offer_status(db: Session, identity: ChannelIdentityDB, offer: P2POfferDB, new_status: OfferStatus) -> P2POfferDB:
    """Only the offer's own owner (or the matched counterparty, for a
    shared trade) can transition it, and only along the explicit
    _ALLOWED_TRANSITIONS map -- never an arbitrary status jump."""
    require_active_membership(db, identity)

    counterparty_id = None
    if offer.matched_offer_id is not None:
        matched = db.query(P2POfferDB).filter(P2POfferDB.id == offer.matched_offer_id).first()
        counterparty_id = matched.channel_identity_id if matched else None

    if identity.id not in (offer.channel_identity_id, counterparty_id):
        raise OfferError("not a party to this offer")

    allowed = _ALLOWED_TRANSITIONS.get(offer.status, set())
    if new_status not in allowed:
        raise OfferError(f"cannot move from {offer.status.value} to {new_status.value}")

    old_status = offer.status
    offer.status = new_status
    offer.updated_at = timeutils.utcnow()
    log_security_event(
        db,
        action=SecurityEventType.TELEGRAM_OFFER_STATUS_CHANGED,
        actor_user_id=None,
        target_type="p2p_offer",
        target_id=offer.id,
        reason=f"{old_status.value} -> {new_status.value}",
    )
    db.commit()
    db.refresh(offer)
    return offer
