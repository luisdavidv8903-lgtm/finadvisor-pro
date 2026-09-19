"""
Safe, templated WhatsApp notifications (Phase 2C section 8). Every template
here is a closed set of pre-written strings with only non-sensitive
interpolation (masked wallet, order reference number, status word) -- never
an f-string built from a settlement payload, evidence note, or dispute
reason. If a template ever needs to carry more detail than that, the answer
is a secure deep link (see messaging/router.py's action_url usage), not a
richer template.

`notify_order_event` is deliberately NOT wired into indexer.py -- Phase 2C's
own instructions forbid moving business logic into WhatsApp-specific code,
and the indexer's job (mirroring on-chain state into P2POrderDB) is already
correct and frozen. This module is the notification GLUE a real deployment
would call as a follow-up step after an indexer run (or from the webhook
handler after a WhatsApp-initiated action); the E2E test in this phase calls
it explicitly after each transition to prove the content is safe end to end.
"""
from __future__ import annotations

from enum import Enum

from sqlalchemy.orm import Session

from ..models import P2POrderDB, WhatsAppLinkDB
from .adapter import MessagingAdapter, OutboundMessage


class OrderNotificationEvent(str, Enum):
    CLAIMED = "claimed"
    PAID = "paid"
    SELLER_ACTION_REQUIRED = "seller_action_required"
    DISPUTE_OPENED = "dispute_opened"
    DISPUTE_RESOLVED = "dispute_resolved"
    RELEASED = "released"
    REFUNDED = "refunded"


_TEMPLATES: dict[OrderNotificationEvent, str] = {
    OrderNotificationEvent.CLAIMED: "Tu oferta (orden #{ref}) fue tomada por un comprador. Estado: CLAIMED.",
    OrderNotificationEvent.PAID: "El comprador de la orden #{ref} marco el pago como realizado. Revisa y confirma antes de liberar.",
    OrderNotificationEvent.SELLER_ACTION_REQUIRED: "Accion requerida: la orden #{ref} espera que confirmes/liberes.",
    OrderNotificationEvent.DISPUTE_OPENED: "La orden #{ref} entro en disputa. Un arbitro la revisara.",
    OrderNotificationEvent.DISPUTE_RESOLVED: "La disputa de la orden #{ref} fue resuelta.",
    OrderNotificationEvent.RELEASED: "Orden #{ref}: fondos liberados. Trade completado.",
    OrderNotificationEvent.REFUNDED: "Orden #{ref}: fondos reembolsados.",
}


def _find_linked_whatsapp_id(db: Session, wallet_address: str | None) -> str | None:
    if not wallet_address:
        return None
    from ..models import UserDB

    user = db.query(UserDB).filter(UserDB.wallet_address == wallet_address).first()
    if user is None:
        return None
    link = db.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.user_id == user.id).first()
    return link.whatsapp_id if link else None


def notify_order_event(
    adapter: MessagingAdapter,
    db: Session,
    order: P2POrderDB,
    event: OrderNotificationEvent,
    *,
    recipients: tuple[str, ...] = ("seller", "buyer"),
) -> None:
    """Sends the safe templated notification to whichever of seller/buyer
    have a linked WhatsApp identity (silently skips the other -- not every
    counterparty is required to use WhatsApp). `order.id` (an internal
    database id, never the on-chain order id) is used only as a short
    reference number in the text -- see the module docstring on the "no
    internal ids unnecessarily" rule from section 2: this is the SAME
    reference number the conversation router already shows the user for
    this same order, not a new identifier."""
    text = _TEMPLATES[event].format(ref=order.id)
    for who in recipients:
        wallet = order.seller_wallet if who == "seller" else order.buyer_wallet
        whatsapp_id = _find_linked_whatsapp_id(db, wallet)
        if whatsapp_id:
            adapter.send(OutboundMessage(to=whatsapp_id, text=text))


__all__ = ["OrderNotificationEvent", "notify_order_event"]
