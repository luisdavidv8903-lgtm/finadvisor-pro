"""
Telegram webhook endpoint -- the Telegram counterpart of routers/whatsapp.py.
Verification, idempotency, and dispatch all mirror the WhatsApp router's
shape; only the transport-specific pieces (secret-token header instead of
HMAC signature, Telegram's Update JSON instead of Meta's envelope) differ.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..messaging import get_telegram_router
from ..messaging.telegram_adapter import verify_webhook_secret
from ..messaging.telegram_envelope import parse_telegram_update
from ..models import ProcessedWebhookEventDB
from ..security.rate_limit import RateLimiter, enforce_rate_limit, get_rate_limiter

logger = logging.getLogger("chambeando.telegram")

router = APIRouter(prefix="/telegram", tags=["telegram"])


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
