"""
Chambeando Cloud Clean -- minimal control receiver.

Deliberately does nothing except: verify Meta's webhook handshake, validate
the X-Hub-Signature-256 signature on inbound POSTs, and log a sanitized
summary of what arrived. No ConversationRouter, no database, no outbound
replies, no business logic -- this process exists only to answer one
question: does a real Meta webhook event reach a second, independent app
subscribed to the same dedicated WABA/phone that Chambeando's existing app
cannot get real events delivered to.

Never logs message body text or raw payloads -- only event metadata
(from, message id/type, timestamp), same safe-logging rule the main
Chambeando backend already follows.
"""
import hashlib
import hmac
import logging
import os

from fastapi import FastAPI, HTTPException, Query, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chambeando_clean")

SECRETS_FILE = os.path.join(os.path.dirname(__file__), ".env.clean.secrets")


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
VERIFY_TOKEN = _secrets.get("CLEAN_WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET = _secrets.get("CLEAN_WHATSAPP_APP_SECRET", "")

app = FastAPI(title="Chambeando Cloud Clean")


def verify_webhook_signature(app_secret: str, payload: bytes, signature_header: str | None) -> bool:
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
        logger.warning("CLEAN_WEBHOOK_EVENT signature_valid=False (rejected)")
        raise HTTPException(status_code=403, detail="Firma de webhook invalida")

    try:
        payload = await request.json()
    except ValueError:
        logger.warning("CLEAN_WEBHOOK_EVENT signature_valid=True body_parse=FAILED")
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

    logger.info("CLEAN_WEBHOOK_EVENT signature_valid=True events=%s", summary)
    return {"received": len(summary)}


@app.get("/health")
def health():
    return {"status": "ok", "service": "chambeando-clean"}
