"""
Telegram webhook endpoint -- the Telegram counterpart of routers/whatsapp.py.
Verification, idempotency, and dispatch all mirror the WhatsApp router's
shape; only the transport-specific pieces (secret-token header instead of
HMAC signature, Telegram's Update JSON instead of Meta's envelope) differ.

Revision 2026-09-27 (Telegram-owned VIP offer pilot): a message is routed to
ONE of two completely separate flows, exactly the way routers/whatsapp.py
already splits VIP traffic from its legacy wallet flow --
_is_vip_telegram_message()/_handle_telegram_vip_message_atomically() below
are this file's counterpart of routers/whatsapp.py's _is_vip_message()/
_handle_vip_message_atomically(). The pre-existing legacy branch (the open
COMPRO/VENDO marketplace via TelegramConversationRouter) is UNCHANGED in
behavior -- it still commits the idempotency marker, then calls
handle_inbound(), exactly as before this revision. Only the fast-path
duplicate check moved earlier (a plain SELECT, no commit) so it can run
BEFORE deciding which branch to route into, mirroring routers/whatsapp.py.

Revision 2026-09-27, second pass (reliability audit): the VIP branch no
longer calls TelegramAdapter.send() directly in a bare loop after the
atomic commit -- that swallowed TelegramApiError with nothing durable to
recover from (see telegram_vip_router.py's module docstring and
services/telegram_outbox.py). It now calls
telegram_outbox.attempt_send_one() for each row handle_inbound() staged,
the exact same "commit first, best-effort-attempt-each-row-after" shape
routers/whatsapp.py already uses for its own outbox.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..messaging import get_telegram_router, get_telegram_vip_router
from ..messaging.telegram_adapter import verify_webhook_secret
from ..messaging.telegram_envelope import parse_telegram_update
from ..messaging.telegram_vip_router import TelegramVipRouter
from ..models import Channel, ChannelIdentityDB, ProcessedWebhookEventDB, TelegramOutboundMessageDB, VipWizardState, WhatsAppVipSessionDB
from ..security.rate_limit import RateLimiter, enforce_rate_limit, get_rate_limiter
from ..services import telegram_outbox

logger = logging.getLogger("chambeando.telegram")

router = APIRouter(prefix="/telegram", tags=["telegram"])

_VIP_TOP_LEVEL_COMMANDS = {"OFERTA", "OFERTAS", "MIS OFERTAS", "MISOFERTAS"}
_VIP_PREFIXES = ("VIP ", "ME INTERESA", "CERRAR")


def _looks_like_start_with_payload(upper: str) -> bool:
    """True for '/START <anything>' (a deep-link tap, valid or tampered) --
    but NOT bare '/START' with no payload, which stays with the legacy
    TelegramConversationRouter's own onboarding (unchanged). A malformed
    payload still routes here so TelegramVipRouter's own graceful
    'enlace invalido' reply is what the user sees, never the marketplace's
    unrelated 'no entendi ese mensaje.'"""
    return upper.startswith("/START ") and upper[len("/START "):].strip() != ""


def _is_vip_telegram_message(db: Session, channel_user_id: str, text: str) -> bool:
    """Routes a message to the VIP offer-board flow (telegram_vip_router.py)
    instead of the open-marketplace TelegramConversationRouter -- by command
    keyword, by an offer_<id> deep-link tap, OR because this Telegram user
    is mid-wizard (same three conditions as routers/whatsapp.py's
    _is_vip_message, applied to the VIP-specific WhatsAppVipSessionDB
    table, never TelegramSessionDB)."""
    upper = (text or "").strip().upper()
    if upper in _VIP_TOP_LEVEL_COMMANDS or upper.startswith(_VIP_PREFIXES) or _looks_like_start_with_payload(upper):
        return True
    identity = db.query(ChannelIdentityDB).filter(ChannelIdentityDB.channel == Channel.TELEGRAM, ChannelIdentityDB.channel_user_id == channel_user_id).first()
    if identity is None:
        return False
    session = db.query(WhatsAppVipSessionDB).filter(WhatsAppVipSessionDB.channel_identity_id == identity.id).first()
    return session is not None and session.state != VipWizardState.IDLE


def _handle_telegram_vip_message_atomically(
    db: Session, vip_router: TelegramVipRouter, channel_user_id: str, text: str, message_id: str
) -> tuple[bool, list[TelegramOutboundMessageDB]]:
    """The atomic unit for one VIP inbound Telegram message: the wizard/
    session state advance, every outbox PENDING row it staged, and the
    webhook idempotency marker -- committed together, exactly once -- same
    shape and same rationale as routers/whatsapp.py's
    _handle_vip_message_atomically. The Telegram Bot API is never called in
    here; the caller attempts each returned row only AFTER this commit
    succeeds. won=False means this call lost a race to a concurrent
    delivery of the SAME message_id that landed first -- everything this
    call staged (state advance AND outbox rows) is rolled back with it,
    never a partial duplicate."""
    try:
        pending_sends = vip_router.handle_inbound(db, channel_user_id, text, message_id=message_id)
        db.add(ProcessedWebhookEventDB(provider="telegram", message_id=message_id))
        db.commit()
    except IntegrityError:
        db.rollback()
        return False, []
    except Exception:
        db.rollback()
        raise
    return True, pending_sends


@router.post("/webhook", status_code=200)
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not verify_webhook_secret(secret_header, settings.TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Firma de webhook invalida")

    try:
        raw = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Payload de webhook invalido")

    message = parse_telegram_update(raw)
    if message is None:
        return {"processed": 0, "duplicates": 0}

    enforce_rate_limit(limiter, f"telegram-inbound:{message.channel_user_id}", settings.RATE_LIMIT_TELEGRAM_MESSAGE_PER_MINUTE)

    # Fast-path duplicate check ONLY (a plain SELECT, no commit) -- mirrors
    # routers/whatsapp.py: lets the VIP-vs-legacy routing decision below
    # happen before either branch's own commit.
    already_processed = (
        db.query(ProcessedWebhookEventDB)
        .filter(ProcessedWebhookEventDB.provider == "telegram", ProcessedWebhookEventDB.message_id == message.message_id)
        .first()
        is not None
    )
    if already_processed:
        logger.info("telegram webhook: duplicate message_id, skipped")
        return {"processed": 0, "duplicates": 1}

    if _is_vip_telegram_message(db, message.channel_user_id, message.text):
        vip_router_instance = get_telegram_vip_router()
        won, pending_sends = _handle_telegram_vip_message_atomically(db, vip_router_instance, message.channel_user_id, message.text, message.message_id)
        if not won:
            logger.info("telegram webhook: duplicate message_id, skipped")
            return {"processed": 0, "duplicates": 1}
        # Immediate best-effort attempt for each row THIS call staged --
        # durable either way (SENT, or left PENDING/FAILED_RETRYABLE for the
        # periodic/startup recovery sweep, see services/telegram_outbox.py
        # and main.py's sweep loop). Never a blind resend of anything
        # already in the outbox from an earlier message.
        for row in pending_sends:
            telegram_outbox.attempt_send_one(db, row, vip_router_instance.telegram_client)
        return {"processed": 1, "duplicates": 0}

    # Legacy open-marketplace flow -- deliberately UNCHANGED by this
    # revision, out of scope (TelegramConversationRouter.handle_inbound
    # commits internally as it goes, and the marker below is a SEPARATE,
    # later commit, same as before).
    event = ProcessedWebhookEventDB(provider="telegram", message_id=message.message_id)
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info("telegram webhook: duplicate message_id, skipped")
        return {"processed": 0, "duplicates": 1}

    get_telegram_router().handle_inbound(db, message.channel_user_id, message.text)
    return {"processed": 1, "duplicates": 0}
