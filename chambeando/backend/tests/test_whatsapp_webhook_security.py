"""
Phase 2C — webhook security + idempotency tests (section 6/13), against the
real HTTP routes (GET/POST /whatsapp/webhook) via TestClient.
"""
import hashlib
import hmac
import json

import pytest

from backend.config import settings
from backend.messaging import ConversationRouter, WhatsAppAdapter, reset_conversation_router_for_tests, set_conversation_router_for_tests
from backend.messaging.whatsapp_adapter import SandboxMetaClient
from backend.models import ProcessedWebhookEventDB
from backend.security.rate_limit import reset_rate_limiter_for_tests


@pytest.fixture()
def sandbox_client():
    client = SandboxMetaClient()
    set_conversation_router_for_tests(ConversationRouter(WhatsAppAdapter(client)))
    reset_rate_limiter_for_tests()
    yield client
    reset_conversation_router_for_tests()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payload(message_id: str, whatsapp_id: str = "wa-webhook-1", text: str = "START") -> bytes:
    return json.dumps({"messages": [{"message_id": message_id, "from_whatsapp_id": whatsapp_id, "text": text}]}).encode()


# ---------------------------------------------------------------------------
# GET verification handshake
# ---------------------------------------------------------------------------


def test_webhook_verification_handshake_succeeds_with_correct_token(client):
    r = client.get(
        "/whatsapp/webhook",
        params={"hub_mode": "subscribe", "hub_verify_token": settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN, "hub_challenge": "echo-me-back"},
    )
    assert r.status_code == 200
    assert r.text == "echo-me-back"


def test_webhook_verification_handshake_fails_with_wrong_token(client):
    r = client.get(
        "/whatsapp/webhook",
        params={"hub_mode": "subscribe", "hub_verify_token": "wrong-token", "hub_challenge": "echo-me-back"},
    )
    assert r.status_code == 403


def test_webhook_verification_handshake_fails_with_wrong_mode(client):
    r = client.get(
        "/whatsapp/webhook",
        params={"hub_mode": "unsubscribe", "hub_verify_token": settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN, "hub_challenge": "echo-me-back"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST signature verification
# ---------------------------------------------------------------------------


def test_webhook_rejects_missing_signature(client, sandbox_client):
    body = _payload("msg-nosig-1")
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 403
    assert sandbox_client.sent == []  # never dispatched to the conversation router


def test_webhook_rejects_wrong_signature(client, sandbox_client):
    body = _payload("msg-wrongsig-1")
    r = client.post(
        "/whatsapp/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert r.status_code == 403
    assert sandbox_client.sent == []


def test_webhook_rejects_tampered_body_with_valid_signature_for_different_body(client, sandbox_client):
    original = _payload("msg-tamper-1")
    signature = _sign(original)
    tampered = _payload("msg-tamper-1", text="SELL")  # same id, different content, old signature
    r = client.post("/whatsapp/webhook", content=tampered, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert r.status_code == 403
    assert sandbox_client.sent == []


def test_webhook_accepts_valid_signature_and_dispatches(client, sandbox_client):
    body = _payload("msg-valid-1")
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json()["processed"] == 1
    assert len(sandbox_client.sent) == 1
    assert sandbox_client.sent[0][0] == "wa-webhook-1"


# ---------------------------------------------------------------------------
# Idempotency / duplicate delivery
# ---------------------------------------------------------------------------


def test_duplicate_webhook_delivery_is_a_noop(client, sandbox_client, db_session):
    body = _payload("msg-dup-1")
    signature = _sign(body)

    first = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert first.status_code == 200
    assert first.json() == {"processed": 1, "duplicates": 0}

    second = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert second.status_code == 200
    assert second.json() == {"processed": 0, "duplicates": 1}

    # only ONE message was actually dispatched to the conversation router
    assert len(sandbox_client.sent) == 1
    assert db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id == "msg-dup-1").count() == 1


def test_two_different_message_ids_both_processed(client, sandbox_client):
    body1 = _payload("msg-multi-1")
    body2 = _payload("msg-multi-2")
    client.post("/whatsapp/webhook", content=body1, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body1)})
    client.post("/whatsapp/webhook", content=body2, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body2)})
    assert len(sandbox_client.sent) == 2


def test_processed_webhook_events_dedupe_constraint_is_db_enforced(db_session):
    """The Python-level check in the webhook handler is convenience -- the
    REAL guarantee is the unique constraint itself."""
    from sqlalchemy.exc import IntegrityError

    db_session.add(ProcessedWebhookEventDB(provider="whatsapp", message_id="dup-check-1"))
    db_session.commit()
    db_session.add(ProcessedWebhookEventDB(provider="whatsapp", message_id="dup-check-1"))
    with pytest.raises(IntegrityError):
        db_session.commit()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_webhook_rate_limits_per_whatsapp_id(client, sandbox_client):
    for i in range(settings.RATE_LIMIT_WHATSAPP_MESSAGE_PER_MINUTE):
        body = _payload(f"msg-rl-{i}", whatsapp_id="wa-ratelimit-1")
        r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
        assert r.status_code == 200

    over_limit_body = _payload("msg-rl-over", whatsapp_id="wa-ratelimit-1")
    r = client.post(
        "/whatsapp/webhook", content=over_limit_body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(over_limit_body)}
    )
    assert r.status_code == 429


# ---------------------------------------------------------------------------
# Safe logging — never dumps the message body
# ---------------------------------------------------------------------------


def test_webhook_handler_never_logs_message_text(client, sandbox_client, caplog):
    secret_text = "SYNTHETIC_MARKER_SHOULD_NEVER_BE_LOGGED"
    body = _payload("msg-nolog-1", text=secret_text)
    with caplog.at_level("DEBUG"):
        client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert secret_text not in caplog.text
