"""
Chambeando Cleanroom (V4) -- minimal isolated receiver.

Same purpose/shape as chambeando-clean/main.py: verify Meta's webhook
handshake, validate X-Hub-Signature-256, log a sanitized summary only. No
ConversationRouter, no database, no outbound replies, no business logic.

Exists only to answer: does a brand-new Meta App (own App ID, own token, own
webhook, own tunnel) receive real events for the shared Test WABA/number --
isolating the app/webhook layer from the dedicated-WABA incident under
investigation. Never logs message body text or raw payloads.
"""
import hashlib
import hmac
import logging
import os

from fastapi import FastAPI, HTTPException, Query, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chambeando_v4")

SECRETS_FILE = os.path.join(os.path.dirname(__file__), ".env.v4.secrets")


def _load_secrets() -> dict[str, str]:
    values: dict[str, str] = {}
    with open(SECRETS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key] = value
    return values


_secrets = _load_secrets()
VERIFY_TOKEN = _secrets.get("V4_WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET = _secrets.get("V4_WHATSAPP_APP_SECRET", "")

# App Secret is gated behind Meta's password re-confirmation dialog, which no
# automated agent may fill in -- explicitly authorized fallback, scoped ONLY
# to this receiver (port 8002 / chambeando-v4-webhook.link-credit.com): skip
# signature validation until a real APP_SECRET is filled in. Never applies to
# any other Chambeando backend/receiver.
SIGNATURE_VALIDATION_DISABLED = not APP_SECRET or APP_SECRET == "REPLACE_ME_FROM_META_DASHBOARD"
if SIGNATURE_VALIDATION_DISABLED:
    logger.warning("V4_SIGNATURE_VALIDATION_TEMPORARILY_DISABLED=YES -- no APP_SECRET configured yet")

app = FastAPI(title="Chambeando Cleanroom V4")


def verify_webhook_signature(app_secret: str, payload: bytes, signature_header: str | None) -> bool:
    if SIGNATURE_VALIDATION_DISABLED:
        return True
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256=") :]
    return hmac.compare_digest(expected, provided)


@app.get("/whatsapp/webhook")
def verify_subscription(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    ok = hub_mode == "subscribe" and hub_verify_token is not None and hmac.compare_digest(
        hub_verify_token, VERIFY_TOKEN
    )
    if not ok:
        raise HTTPException(status_code=403, detail="Verificacion de webhook fallida")
    return Response(content=hub_challenge or "", media_type="text/plain")


@app.post("/whatsapp/webhook", status_code=200)
async def receive_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    signature_valid = verify_webhook_signature(APP_SECRET, raw_body, signature)

    if not signature_valid:
        logger.warning("V4_WEBHOOK_EVENT signature_valid=False (rejected)")
        raise HTTPException(status_code=403, detail="Firma de webhook invalida")

    try:
        payload = await request.json()
    except ValueError:
        logger.warning("V4_WEBHOOK_EVENT signature_valid=True body_parse=FAILED")
        raise HTTPException(status_code=400, detail="Payload de webhook invalido")

    # Sanitized summary only -- never the message body/text, never the raw payload.
    entries = payload.get("entry", [])
    summary = []
    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                summary.append(
                    {
                        "from": msg.get("from"),
                        "message_id": msg.get("id"),
                        "type": msg.get("type"),
                        "timestamp": msg.get("timestamp"),
                    }
                )
            for status in value.get("statuses", []):
                summary.append(
                    {
                        "status": status.get("status"),
                        "message_id": status.get("id"),
                        "recipient_id": status.get("recipient_id"),
                        "timestamp": status.get("timestamp"),
                    }
                )

    logger.info("V4_WEBHOOK_EVENT signature_valid=True events=%s", summary)
    return {"received": len(summary)}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "chambeando-v4-cleanroom",
        "signature_validation_disabled": SIGNATURE_VALIDATION_DISABLED,
    }
