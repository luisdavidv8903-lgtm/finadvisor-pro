"""
Phase 2C — unit tests for the WhatsApp messaging adapter (section 13):
MessagingAdapter/WhatsAppAdapter/SandboxMetaClient, webhook signature
verification, webhook subscription-handshake verification, and the identity
mapping (messaging/identity.py) in isolation from any HTTP route.
"""
from datetime import timedelta

import pytest

from backend import timeutils
from backend.messaging.adapter import OutboundMessage
from backend.messaging.identity import (
    LinkTokenError,
    consume_link_token,
    get_linked_user,
    get_or_create_link,
    issue_link_token,
)
from backend.messaging.whatsapp_adapter import SandboxMetaClient, WhatsAppAdapter, verify_webhook_signature, verify_webhook_subscription
from backend.models import UserDB


# ---------------------------------------------------------------------------
# SandboxMetaClient / WhatsAppAdapter
# ---------------------------------------------------------------------------


def test_sandbox_client_never_calls_network_only_records():
    client = SandboxMetaClient()
    adapter = WhatsAppAdapter(client)
    adapter.send(OutboundMessage(to="wa-1", text="hola"))
    assert client.sent == [("wa-1", "hola")]


def test_adapter_appends_action_url_to_message_body():
    client = SandboxMetaClient()
    adapter = WhatsAppAdapter(client)
    adapter.send(OutboundMessage(to="wa-1", text="firma aqui", action_url="https://chambeando.local/app/link?token=abc"))
    to, text = client.sent[0]
    assert to == "wa-1"
    assert "firma aqui" in text
    assert "https://chambeando.local/app/link?token=abc" in text


# ---------------------------------------------------------------------------
# Webhook signature verification (real HMAC-SHA256, synthetic secret)
# ---------------------------------------------------------------------------

APP_SECRET = "sandbox-test-secret-never-real"


def _sign(payload: bytes) -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(APP_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def test_valid_signature_accepted():
    payload = b'{"messages": []}'
    assert verify_webhook_signature(APP_SECRET, payload, _sign(payload)) is True


def test_tampered_payload_rejected():
    payload = b'{"messages": []}'
    signature = _sign(payload)
    tampered = b'{"messages": [{"injected": true}]}'
    assert verify_webhook_signature(APP_SECRET, tampered, signature) is False


def test_wrong_secret_rejected():
    payload = b'{"messages": []}'
    import hashlib
    import hmac

    wrong_signature = "sha256=" + hmac.new(b"wrong-secret", payload, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(APP_SECRET, payload, wrong_signature) is False


def test_missing_signature_rejected():
    assert verify_webhook_signature(APP_SECRET, b"{}", None) is False


def test_malformed_signature_header_rejected():
    assert verify_webhook_signature(APP_SECRET, b"{}", "not-a-valid-header") is False
    assert verify_webhook_signature(APP_SECRET, b"{}", "sha256=") is False


def test_subscription_handshake_requires_matching_mode_and_token():
    assert verify_webhook_subscription("subscribe", "correct-token", "correct-token") is True
    assert verify_webhook_subscription("subscribe", "wrong-token", "correct-token") is False
    assert verify_webhook_subscription("unsubscribe", "correct-token", "correct-token") is False
    assert verify_webhook_subscription(None, None, "correct-token") is False


# ---------------------------------------------------------------------------
# Identity mapping (messaging/identity.py)
# ---------------------------------------------------------------------------


def _make_user(db_session, wallet: str) -> UserDB:
    user = UserDB(wallet_address=wallet)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_get_or_create_link_is_idempotent(db_session):
    link1 = get_or_create_link(db_session, "wa-idempotent-1")
    link2 = get_or_create_link(db_session, "wa-idempotent-1")
    assert link1.id == link2.id


def test_unlinked_whatsapp_id_has_no_user(db_session):
    get_or_create_link(db_session, "wa-unlinked-1")
    assert get_linked_user(db_session, "wa-unlinked-1") is None


def test_never_seen_whatsapp_id_has_no_user(db_session):
    assert get_linked_user(db_session, "wa-never-seen") is None


def test_issue_and_consume_link_token_succeeds(db_session):
    user = _make_user(db_session, "0xWhatsAppLinkUser1")
    token = issue_link_token(db_session, "wa-link-success-1", ttl_seconds=600)
    link = consume_link_token(db_session, token, user)
    assert link.user_id == user.id
    assert link.linked_at is not None
    assert get_linked_user(db_session, "wa-link-success-1").id == user.id


def test_link_token_is_single_use(db_session):
    user = _make_user(db_session, "0xWhatsAppLinkUser2")
    token = issue_link_token(db_session, "wa-link-single-use-1", ttl_seconds=600)
    consume_link_token(db_session, token, user)
    with pytest.raises(LinkTokenError):
        consume_link_token(db_session, token, user)


def test_expired_link_token_rejected(db_session, monkeypatch):
    user = _make_user(db_session, "0xWhatsAppLinkUser3")
    frozen_now = timeutils.utcnow()
    monkeypatch.setattr(timeutils, "utcnow", lambda: frozen_now)
    token = issue_link_token(db_session, "wa-link-expired-1", ttl_seconds=60)

    monkeypatch.setattr(timeutils, "utcnow", lambda: frozen_now + timedelta(seconds=61))
    with pytest.raises(LinkTokenError):
        consume_link_token(db_session, token, user)


def test_invalid_link_token_rejected(db_session):
    user = _make_user(db_session, "0xWhatsAppLinkUser4")
    with pytest.raises(LinkTokenError):
        consume_link_token(db_session, "this-token-was-never-issued", user)


def test_link_token_cannot_be_stolen_by_a_different_wallet(db_session):
    """Section 5: a WhatsApp identity's link token is bound to whichever
    wallet completes the /auth/verify + /whatsapp/link flow FIRST -- a second,
    different wallet cannot redeem the same (already-consumed) token, and
    re-using the token string at all fails since it's single-use."""
    owner = _make_user(db_session, "0xWhatsAppLinkOwner")
    attacker = _make_user(db_session, "0xWhatsAppLinkAttacker")
    token = issue_link_token(db_session, "wa-link-theft-1", ttl_seconds=600)
    consume_link_token(db_session, token, owner)
    with pytest.raises(LinkTokenError):
        consume_link_token(db_session, token, attacker)
