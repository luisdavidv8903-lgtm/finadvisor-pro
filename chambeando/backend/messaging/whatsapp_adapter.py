"""
WhatsApp implementation of MessagingAdapter (Phase 2C) -- sandbox only. The
Meta Cloud API client itself is abstracted behind `MetaClient` so this phase
can ship a `SandboxMetaClient` (a pure in-memory fake, exactly like
FakeChainAdapter for the chain: it makes no network call, ever, and records
every send for inspection) without any code path that could accidentally
reach a real Meta endpoint. A real deployment plugs in a genuine
`MetaClient` implementation; nothing above this file changes.

Also holds the two pieces of real webhook security this phase implements
(Phase 2C section 6): payload signature verification (HMAC-SHA256, the
scheme Meta's Cloud API actually uses for `X-Hub-Signature-256`) and the
webhook-subscription verification handshake (`hub.mode`/`hub.verify_token`/
`hub.challenge`). Both are pure functions, real crypto, tested against
synthetic secrets -- never a production app secret.
"""
from __future__ import annotations

import hashlib
import hmac
from abc import ABC, abstractmethod

from .adapter import MessagingAdapter, OutboundMessage


class MetaClient(ABC):
    """Port for the actual Meta Cloud API send call. Only method business
    code (WhatsAppAdapter) ever calls -- never `requests`/`httpx` directly."""

    @abstractmethod
    def send_message(self, to: str, text: str) -> None: ...


class SandboxMetaClient(MetaClient):
    """In-memory fake -- never makes an HTTP call. `sent` is a plain list a
    test can inspect directly, same role as FakeChainAdapter's `_events`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_message(self, to: str, text: str) -> None:
        self.sent.append((to, text))


class WhatsAppAdapter(MessagingAdapter):
    def __init__(self, client: MetaClient | None = None) -> None:
        self._client = client or SandboxMetaClient()

    def send(self, message: OutboundMessage) -> None:
        text = message.text
        if message.action_url:
            text = f"{text}\n{message.action_url}"
        self._client.send_message(message.to, text)


def verify_webhook_signature(app_secret: str, payload: bytes, signature_header: str | None) -> bool:
    """Meta signs the raw request body with HMAC-SHA256 keyed by the app
    secret, sent as `X-Hub-Signature-256: sha256=<hex>`. Constant-time
    compare -- a naive `==` here would reopen exactly the timing side-channel
    every other secret comparison in this codebase already avoids."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256=") :]
    return hmac.compare_digest(expected, provided)


def verify_webhook_subscription(mode: str | None, token: str | None, expected_token: str) -> bool:
    """The one-time GET handshake Meta performs when a webhook URL is first
    registered -- must echo back hub.challenge ONLY if mode=="subscribe" and
    the token matches, otherwise an attacker could probe for the endpoint's
    existence for free."""
    return mode == "subscribe" and token is not None and hmac.compare_digest(token, expected_token)
