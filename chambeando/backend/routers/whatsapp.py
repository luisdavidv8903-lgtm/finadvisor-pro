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
from ..deps import get_authenticated_network, get_current_user
from ..messaging import get_conversation_router, get_whatsapp_vip_router
from ..messaging.identity import LinkTokenError, consume_link_token
from ..messaging.meta_envelope import MetaWebhookEnvelope, parse_meta_webhook_envelope, parse_meta_webhook_statuses
from ..messaging.whatsapp_adapter import verify_webhook_signature, verify_webhook_subscription, webhook_signature_check_is_trustworthy
from ..messaging.whatsapp_vip_router import WhatsAppVipRouter
from ..models import Channel, ChannelIdentityDB, OutboundMessageDB, ProcessedWebhookEventDB, SecurityEventType, UserDB, VipWizardState, WhatsAppVipSessionDB
from ..schemas import WhatsAppLinkRequest, WhatsAppLinkResponse
from ..security.audit import log_security_event
from ..security.rate_limit import RateLimiter, enforce_rate_limit, get_rate_limiter
from ..services import whatsapp_outbox

logger = logging.getLogger("chambeando.whatsapp")

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

_VIP_TOP_LEVEL_COMMANDS = {"OFERTA", "OFERTAS", "MIS OFERTAS", "MISOFERTAS"}
_VIP_PREFIXES = ("VIP ", "ME INTERESA", "CERRAR")


def _is_vip_message(db: Session, whatsapp_id: str, text: str) -> bool:
    """Routes a message to the VIP offer-board flow (whatsapp_vip_router.py)
    instead of the wallet/escrow ConversationRouter -- by command keyword, OR
    because this whatsapp_id is mid-wizard (a numeric/free-text answer to a
    guided question would otherwise fall through to the wrong router's
    "No entendi ese mensaje" instead of continuing the wizard)."""
    upper = (text or "").strip().upper()
    if upper in _VIP_TOP_LEVEL_COMMANDS or upper.startswith(_VIP_PREFIXES):
        return True
    identity = db.query(ChannelIdentityDB).filter(ChannelIdentityDB.channel == Channel.WHATSAPP, ChannelIdentityDB.channel_user_id == whatsapp_id).first()
    if identity is None:
        return False
    session = db.query(WhatsAppVipSessionDB).filter(WhatsAppVipSessionDB.channel_identity_id == identity.id).first()
    return session is not None and session.state != VipWizardState.IDLE


def _handle_vip_message_atomically(db: Session, vip_router: WhatsAppVipRouter, whatsapp_id: str, text: str, message_id: str) -> tuple[bool, list[OutboundMessageDB]]:
    """The atomic unit for one VIP inbound message: the wizard/session state
    advance, every outbox PENDING row it staged, and the webhook idempotency
    marker -- committed together, exactly once (revision 2026-09-27 audit
    finding; see whatsapp_vip_router.py's module docstring for the full
    rationale). Meta is never called in here.

    Returns (won, pending_sends). won=False means this call's commit lost a
    race to a concurrent delivery of the SAME message_id that got there
    first (IntegrityError on the marker's unique constraint) -- everything
    THIS call staged (state advance included) is rolled back with it, never
    a partial duplicate. Any other exception is also rolled back before
    propagating, exactly like a real process crash: nothing of this attempt
    survives, so a redelivery replays cleanly from the unchanged prior
    state. Extracted as its own function (rather than inlined in
    receive_webhook) so it can be exercised directly -- with two independent
    DB sessions on two real threads -- by the concurrency tests, without
    going through the HTTP layer."""
    try:
        pending_sends = vip_router.handle_inbound(db, whatsapp_id, text, message_id=message_id)
        db.add(ProcessedWebhookEventDB(provider="whatsapp", message_id=message_id))
        db.commit()
    except IntegrityError:
        db.rollback()
        return False, []
    except Exception:
        db.rollback()
        raise
    return True, pending_sends


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
    # Fail closed BEFORE looking at any signature: if WHATSAPP_APP_SECRET is
    # still the public placeholder from config.py, a "valid" signature proves
    # nothing (see webhook_signature_check_is_trustworthy's docstring) -- so
    # every POST is rejected here unless an operator deliberately opted in.
    if not webhook_signature_check_is_trustworthy(settings.WHATSAPP_APP_SECRET, settings.WHATSAPP_ALLOW_UNVERIFIED_WEBHOOKS):
        logger.error("whatsapp webhook: rejected -- WHATSAPP_APP_SECRET is still the public sandbox default")
        raise HTTPException(status_code=503, detail="Webhook no configurado de forma segura")

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
    # SAME envelope -- never converted into inbound conversational input
    # (parse_meta_webhook_envelope), but the VIP outbox DOES want them to
    # correlate delivery confirmation (parse_meta_webhook_statuses, below).
    inbound_messages = parse_meta_webhook_envelope(envelope)

    router_instance = get_conversation_router()
    vip_router_instance = get_whatsapp_vip_router()
    processed = 0
    duplicates = 0

    for message in inbound_messages:
        enforce_rate_limit(limiter, f"whatsapp-inbound:{message.whatsapp_id}", settings.RATE_LIMIT_WHATSAPP_MESSAGE_PER_MINUTE)

        # Fast-path duplicate check ONLY -- NOT the real guarantee (see
        # below). A message already marked processed is skipped without
        # re-running the handler or re-sending a reply.
        already_processed = (
            db.query(ProcessedWebhookEventDB)
            .filter(ProcessedWebhookEventDB.provider == "whatsapp", ProcessedWebhookEventDB.message_id == message.message_id)
            .first()
            is not None
        )
        if already_processed:
            duplicates += 1
            logger.info("whatsapp webhook: duplicate message_id, skipped")
            continue

        if _is_vip_message(db, message.whatsapp_id, message.text):
            # See _handle_vip_message_atomically's docstring and
            # whatsapp_vip_router.py's module docstring: the wizard/session
            # state advance, every outbox row it staged, and the marker
            # below all land in ONE commit, and Meta is only ever called
            # AFTER that commit succeeds -- for exactly the rows THIS call
            # staged, never a blind resend of anything already in the
            # outbox from an earlier message.
            won, pending_sends = _handle_vip_message_atomically(db, vip_router_instance, message.whatsapp_id, message.text, message.message_id)
            if not won:
                duplicates += 1
                logger.info("whatsapp webhook: duplicate message_id, skipped")
                continue
            for row in pending_sends:
                whatsapp_outbox.attempt_send_one(db, row, vip_router_instance.meta_client)
        else:
            # Legacy wallet/escrow flow -- deliberately UNCHANGED by this
            # fix, out of scope (ConversationRouter.handle_inbound commits
            # internally as it goes, same as telegram_router.py/router.py,
            # and the marker below is a SEPARATE, later commit). The known
            # limit this leaves in place: a crash between that internal
            # commit and the marker commit below is safe for THIS flow only
            # because every wallet-side mutation it can trigger is already
            # independently idempotent by its own DB constraint (same
            # reasoning VIP's create_offer/register_interest/close_offer
            # rely on) -- there is no multi-step numeric wizard on this side
            # for an intermediate answer to be misapplied to.
            router_instance.handle_inbound(db, message.whatsapp_id, message.text)
            event = ProcessedWebhookEventDB(provider="whatsapp", message_id=message.message_id)
            db.add(event)
            try:
                db.commit()
            except IntegrityError:
                # Lost a race with a concurrent delivery of the same
                # message_id that finished first -- our own handler run
                # already completed safely (idempotent, see above); nothing
                # left to do.
                db.rollback()
        processed += 1

    status_updates = parse_meta_webhook_statuses(envelope)
    for status_update in status_updates:
        whatsapp_outbox.apply_status_update(db, status_update.wamid, status_update.status)

    logger.info("whatsapp webhook: processed=%s duplicates=%s status_updates=%s", processed, duplicates, len(status_updates))
    return {"processed": processed, "duplicates": duplicates}


@router.post("/link", response_model=WhatsAppLinkResponse)
def link_whatsapp_identity(
    payload: WhatsAppLinkRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
    authenticated_network: str = Depends(get_authenticated_network),
):
    """Called by the (mocked, in this phase) wallet-signing web page AFTER
    the user already completed the normal /auth/nonce + /auth/verify flow --
    get_current_user is the SAME dependency every other authenticated route
    uses; this endpoint proves nothing about wallet ownership itself, it only
    binds an already-proven identity to the WhatsApp id that issued the
    link_token (see messaging/identity.py).

    `authenticated_network` (revision 2026-09-25) closes a gap
    `payload.network_id` alone left open: that field is just a CLIENT
    ASSERTION echoed back from the deep link, and consume_link_token() only
    ever checked it against link_token_network_id -- it never checked
    against what network the CURRENT JWT actually proved. A client could in
    principle authenticate on network A and then assert network_id=B in this
    body, matching a link_token genuinely issued for B, without ever having
    proven ownership of a B-family wallet at all. Comparing
    authenticated_network (read straight from the signed JWT, never
    client-suppliable) against payload.network_id closes that -- both must
    agree, on top of consume_link_token()'s existing link_token_network_id
    check, before any linking happens."""
    if authenticated_network != payload.network_id:
        raise HTTPException(
            status_code=400,
            detail="network mismatch: the authenticated session's network does not match the network requested for this link",
        )
    try:
        link = consume_link_token(db, payload.link_token, current_user, network_id=payload.network_id)
        log_security_event(
            db,
            action=SecurityEventType.WHATSAPP_ACCOUNT_LINKED,
            actor_user_id=current_user.id,
            target_type="whatsapp_link",
            target_id=str(link.id),
        )
        db.commit()
    except LinkTokenError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
    except Exception:
        # Includes audit flush/commit failures: preserve the token for retry.
        db.rollback()
        raise

    get_conversation_router().notify_link_complete(db, link.whatsapp_id)

    return WhatsAppLinkResponse(whatsapp_id=link.whatsapp_id, linked=True)
