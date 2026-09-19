"""
WhatsApp sandbox webhook + wallet-link glue (Phase 2C). Deliberately thin:
webhook verification/signature/idempotency/rate-limiting live here (real
security concerns of THIS layer), but every product decision is delegated to
messaging.router.ConversationRouter, which itself only calls existing
Chambeando services -- nothing here re-implements membership, invite,
settlement, or dispute logic.

Safe-logging rule (section 6): this module never logs a message body/text --
only event metadata (whatsapp_id, message_id, processed/duplicate) the same
way security/audit.py never logs a sensitive VALUE, only WHO/WHAT/WHEN.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..messaging import get_conversation_router
from ..messaging.identity import LinkTokenError, consume_link_token
from ..messaging.meta_envelope import MetaWebhookEnvelope, parse_meta_webhook_envelope
from ..messaging.whatsapp_adapter import verify_webhook_signature, verify_webhook_subscription
from ..models import ProcessedWebhookEventDB, SecurityEventType, UserDB
from ..schemas import WhatsAppLinkRequest, WhatsAppLinkResponse
from ..security.audit import log_security_event
from ..security.rate_limit import RateLimiter, enforce_rate_limit, get_rate_limiter

logger = logging.getLogger("chambeando.whatsapp")

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


@router.get("/webhook")
def verify_subscription(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    """The one-time GET handshake Meta performs when registering a webhook
    URL. Meta sends the real dotted query param names (hub.mode,
    hub.verify_token, hub.challenge) -- FastAPI can bind a dotted query
    param to a normal Python identifier via Query(alias=...), which is what
    happens here; no proxy/edge rewrite is needed. The verify token itself
    is never echoed back in any response (only hub_challenge, which is not
    secret -- Meta generates it per-handshake specifically to be echoed)."""
    if not verify_webhook_subscription(hub_mode, hub_verify_token, settings.WHATSAPP_VERIFY_TOKEN):
        raise HTTPException(status_code=403, detail="Verificacion de webhook fallida")
    return Response(content=hub_challenge or "", media_type="text/plain")


@router.post("/webhook", status_code=200)
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    # Signature verification happens against the EXACT raw bytes Meta sent,
    # before any JSON parsing/transformation could alter what's being
    # verified (Phase 2D.1 section 5) -- request.body() is the raw payload.
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_webhook_signature(settings.WHATSAPP_APP_SECRET, raw_body, signature):
        # never echo back the raw body or the signature header — both count
        # as request material, not something a 403 response should mirror
        raise HTTPException(status_code=403, detail="Firma de webhook invalida")

    try:
        envelope = MetaWebhookEnvelope.model_validate_json(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Payload de webhook invalido")

    # `statuses` (delivery/read receipts for messages WE sent) live in the
    # SAME envelope but are never surfaced here — parse_meta_webhook_envelope
    # only ever returns genuine inbound user messages, never a status event
    # mistaken for one (section 6).
    inbound_messages = parse_meta_webhook_envelope(envelope)

    router_instance = get_conversation_router()
    processed = 0
    duplicates = 0

    for message in inbound_messages:
        enforce_rate_limit(limiter, f"whatsapp-inbound:{message.whatsapp_id}", settings.RATE_LIMIT_WHATSAPP_MESSAGE_PER_MINUTE)

        # DB-ENFORCED idempotency: the unique constraint on (provider, message_id)
        # is the actual guarantee, not this Python check -- a genuine race
        # between two webhook deliveries for the same message_id still can't
        # both win (see models.py's ProcessedWebhookEventDB docstring). Meta's
        # own message id (wamid...) is the external idempotency key, exactly
        # as section 6 specifies -- no separate id is generated here.
        event = ProcessedWebhookEventDB(provider="whatsapp", message_id=message.message_id)
        db.add(event)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            duplicates += 1
            logger.info("whatsapp webhook: duplicate message_id, skipped")
            continue

        router_instance.handle_inbound(db, message.whatsapp_id, message.text)
        processed += 1

    logger.info("whatsapp webhook: processed=%s duplicates=%s", processed, duplicates)
    return {"processed": processed, "duplicates": duplicates}


@router.post("/link", response_model=WhatsAppLinkResponse)
def link_whatsapp_identity(
    payload: WhatsAppLinkRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """Called by the (mocked, in this phase) wallet-signing web page AFTER
    the user already completed the normal /auth/nonce + /auth/verify flow --
    get_current_user is the SAME dependency every other authenticated route
    uses; this endpoint proves nothing about wallet ownership itself, it only
    binds an already-proven identity to the WhatsApp id that issued the
    link_token (see messaging/identity.py)."""
    try:
        link = consume_link_token(db, payload.link_token, current_user)
    except LinkTokenError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason)

    log_security_event(
        db,
        action=SecurityEventType.WHATSAPP_ACCOUNT_LINKED,
        actor_user_id=current_user.id,
        target_type="whatsapp_link",
        target_id=str(link.id),
    )
    db.commit()

    get_conversation_router().notify_link_complete(db, link.whatsapp_id)

    return WhatsAppLinkResponse(whatsapp_id=link.whatsapp_id, linked=True)
