"""
Phase 2C/2D.1 — webhook security + idempotency tests (section 6/13), against
the real HTTP routes (GET/POST /whatsapp/webhook) via TestClient, using the
REAL Meta Cloud API webhook envelope shape (Phase 2D.1 replaced Phase 2C's
simplified synthetic payload).
"""
import hashlib
import hmac
import json

import pytest

from backend.config import settings
from backend.messaging import ConversationRouter, WhatsAppAdapter, reset_conversation_router_for_tests, set_conversation_router_for_tests
from backend.messaging.whatsapp_adapter import MetaApiError, MetaClient, SandboxMetaClient
from backend.models import ProcessedWebhookEventDB
from backend.security.rate_limit import reset_rate_limiter_for_tests


@pytest.fixture()
def sandbox_client():
    client = SandboxMetaClient()
    set_conversation_router_for_tests(ConversationRouter(WhatsAppAdapter(client)))
    reset_rate_limiter_for_tests()
    yield client
    reset_conversation_router_for_tests()


class _FailingMetaClient(MetaClient):
    """Always raises MetaApiError -- reproduces the real 500 seen from
    Meta's official webhook test button (send_message.py:126) so the
    regression (outbound send failure must never crash the inbound webhook
    response) has a real HTTP-level test, not just the adapter unit test."""

    def send_message(self, to: str, text: str) -> None:
        raise MetaApiError("Meta API returned HTTP 500")


@pytest.fixture()
def failing_send_client():
    set_conversation_router_for_tests(ConversationRouter(WhatsAppAdapter(_FailingMetaClient())))
    reset_rate_limiter_for_tests()
    yield
    reset_conversation_router_for_tests()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _envelope(message_id: str, whatsapp_id: str = "wa-webhook-1", text: str = "START") -> bytes:
    """A real (minimal) Meta Cloud API webhook envelope -- see
    messaging/meta_envelope.py for the full shape this mirrors."""
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "TEST_WABA_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "TEST_PHONE_NUMBER_ID", "display_phone_number": "15550001111"},
                                "contacts": [{"wa_id": whatsapp_id, "profile": {"name": "Synthetic Test User"}}],
                                "messages": [
                                    {"id": message_id, "from": whatsapp_id, "timestamp": "1700000000", "type": "text", "text": {"body": text}}
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def _status_envelope(status_id: str, recipient_id: str = "wa-webhook-1") -> bytes:
    """A real Meta status-update envelope (delivered/read/failed for a
    message WE sent) -- structurally different from an inbound message,
    carried in `value.statuses` instead of `value.messages`."""
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "TEST_WABA_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "TEST_PHONE_NUMBER_ID"},
                                "statuses": [{"id": status_id, "status": "delivered", "recipient_id": recipient_id}],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


# ---------------------------------------------------------------------------
# GET verification handshake — real dotted hub.* query params
# ---------------------------------------------------------------------------


def test_webhook_verification_handshake_succeeds_with_correct_token(client):
    r = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN, "hub.challenge": "echo-me-back"},
    )
    assert r.status_code == 200
    assert r.text == "echo-me-back"


def test_webhook_verification_handshake_fails_with_wrong_token(client):
    r = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong-token", "hub.challenge": "echo-me-back"},
    )
    assert r.status_code == 403


def test_webhook_verification_handshake_fails_with_wrong_mode(client):
    r = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "unsubscribe", "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN, "hub.challenge": "echo-me-back"},
    )
    assert r.status_code == 403


def test_webhook_verification_never_echoes_the_verify_token(client):
    r = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN, "hub.challenge": "echo-me-back"},
    )
    assert settings.WHATSAPP_VERIFY_TOKEN not in r.text


# ---------------------------------------------------------------------------
# POST signature verification (real raw-body HMAC)
# ---------------------------------------------------------------------------


def test_webhook_rejects_missing_signature(client, sandbox_client):
    body = _envelope("wamid.nosig1")
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 403
    assert sandbox_client.sent == []  # never dispatched to the conversation router


def test_webhook_rejects_wrong_signature(client, sandbox_client):
    body = _envelope("wamid.wrongsig1")
    r = client.post(
        "/whatsapp/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert r.status_code == 403
    assert sandbox_client.sent == []


def test_webhook_rejects_tampered_body_with_valid_signature_for_different_body(client, sandbox_client):
    original = _envelope("wamid.tamper1")
    signature = _sign(original)
    tampered = _envelope("wamid.tamper1", text="SELL")  # same id, different content, old signature
    r = client.post("/whatsapp/webhook", content=tampered, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert r.status_code == 403
    assert sandbox_client.sent == []


def test_webhook_accepts_valid_signature_and_dispatches(client, sandbox_client):
    body = _envelope("wamid.valid1")
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json()["processed"] == 1
    assert len(sandbox_client.sent) == 1
    assert sandbox_client.sent[0][0] == "wa-webhook-1"


# ---------------------------------------------------------------------------
# Real envelope parsing — multiple entries/changes, interactive/button,
# statuses ignored, unsupported types safely skipped
# ---------------------------------------------------------------------------


def test_webhook_handles_multiple_entries_and_changes_in_one_delivery(client, sandbox_client):
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_A",
                    "changes": [
                        {"field": "messages", "value": {"messages": [{"id": "wamid.multi1", "from": "wa-multi-1", "type": "text", "text": {"body": "START"}}]}}
                    ],
                },
                {
                    "id": "WABA_B",
                    "changes": [
                        {"field": "messages", "value": {"messages": [{"id": "wamid.multi2", "from": "wa-multi-2", "type": "text", "text": {"body": "START"}}]}},
                        {"field": "messages", "value": {"messages": [{"id": "wamid.multi3", "from": "wa-multi-3", "type": "text", "text": {"body": "START"}}]}},
                    ],
                },
            ],
        }
    ).encode()
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json()["processed"] == 3
    assert {to for to, _ in sandbox_client.sent} == {"wa-multi-1", "wa-multi-2", "wa-multi-3"}


def test_webhook_parses_interactive_button_reply(client, sandbox_client):
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_A",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.button1",
                                        "from": "wa-button-1",
                                        "type": "interactive",
                                        "interactive": {"type": "button_reply", "button_reply": {"id": "BUY", "title": "Buy"}},
                                    }
                                ]
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json()["processed"] == 1
    # dispatched with "BUY" as the conversational text -- membership gate
    # denies it (no linked wallet), but that proves the button reply WAS
    # routed into handle_inbound, not silently dropped
    to, text = sandbox_client.sent[0]
    assert to == "wa-button-1"


def test_webhook_status_updates_are_never_dispatched_as_inbound_messages(client, sandbox_client):
    body = _status_envelope("wamid.status1")
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json() == {"processed": 0, "duplicates": 0}
    assert sandbox_client.sent == []


def test_webhook_unsupported_message_type_acknowledged_without_crashing(client, sandbox_client):
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_A",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {"messages": [{"id": "wamid.image1", "from": "wa-unsupported-1", "type": "image", "image": {"id": "media123"}}]},
                        }
                    ],
                }
            ],
        }
    ).encode()
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json() == {"processed": 0, "duplicates": 0}
    assert sandbox_client.sent == []


def test_webhook_malformed_body_rejected_not_crashed(client, sandbox_client):
    body = b"this is not json at all"
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Idempotency / duplicate delivery
# ---------------------------------------------------------------------------


def test_duplicate_webhook_delivery_is_a_noop(client, sandbox_client, db_session):
    body = _envelope("wamid.dup1")
    signature = _sign(body)

    first = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert first.status_code == 200
    assert first.json() == {"processed": 1, "duplicates": 0}

    second = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert second.status_code == 200
    assert second.json() == {"processed": 0, "duplicates": 1}

    # only ONE message was actually dispatched to the conversation router
    assert len(sandbox_client.sent) == 1
    assert db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id == "wamid.dup1").count() == 1


def test_two_different_message_ids_both_processed(client, sandbox_client):
    body1 = _envelope("wamid.multiid1")
    body2 = _envelope("wamid.multiid2")
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
        body = _envelope(f"wamid.rl{i}", whatsapp_id="wa-ratelimit-1")
        r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
        assert r.status_code == 200

    over_limit_body = _envelope("wamid.rlover", whatsapp_id="wa-ratelimit-1")
    r = client.post(
        "/whatsapp/webhook", content=over_limit_body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(over_limit_body)}
    )
    assert r.status_code == 429


# ---------------------------------------------------------------------------
# Safe logging — never dumps the message body
# ---------------------------------------------------------------------------


def test_webhook_handler_never_logs_message_text(client, sandbox_client, caplog):
    secret_text = "SYNTHETIC_MARKER_SHOULD_NEVER_BE_LOGGED"
    body = _envelope("wamid.nolog1", text=secret_text)
    with caplog.at_level("DEBUG"):
        client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert secret_text not in caplog.text


# ---------------------------------------------------------------------------
# Outbound send failure must never crash the inbound webhook response
# (regression: Meta's official test-webhook button triggered a reply send
# that failed with a real HTTP 500 from Meta's own API, which propagated
# uncaught and turned the inbound ack itself into a 500 -- Meta would then
# treat the whole delivery as failed and keep retrying it).
# ---------------------------------------------------------------------------


def test_webhook_still_acks_200_when_outbound_reply_send_fails(client, failing_send_client):
    body = _envelope("wamid.sendfail1")
    r = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json() == {"processed": 1, "duplicates": 0}


def test_webhook_marks_event_processed_even_when_reply_send_fails(client, failing_send_client, db_session):
    """The inbound message itself was legitimately processed (idempotency
    event recorded, router dispatched) -- only the reply failed to send.
    Meta must not receive a 500 and redeliver, since redelivery would hit
    the DB-enforced idempotency check and be silently skipped as a
    duplicate, permanently losing the user's reply."""
    body = _envelope("wamid.sendfail2")
    client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
    assert db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id == "wamid.sendfail2").count() == 1
