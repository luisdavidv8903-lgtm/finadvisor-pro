"""
WhatsApp implementation of MessagingAdapter (Phase 2C/2D.1). The Meta Cloud
API client itself is abstracted behind `MetaClient` so this codebase can
ship both `SandboxMetaClient` (a pure in-memory fake, exactly like
FakeChainAdapter for the chain: it makes no network call, ever, and records
every send for inspection) and `MetaCloudWhatsAppClient` (the real transport,
Phase 2D.1) behind the SAME interface. `messaging/__init__.get_conversation_router()`
picks which one to construct based on `settings.WHATSAPP_PROVIDER` --
config-driven, never auto-detected from "are credentials present" (see
config.py).

Also holds the two pieces of real webhook security implemented since Phase
2C: payload signature verification (HMAC-SHA256, the scheme Meta's Cloud API
actually uses for `X-Hub-Signature-256`) and the webhook-subscription
verification handshake (`hub.mode`/`hub.verify_token`/`hub.challenge`). Both
are pure functions, real crypto, tested against synthetic secrets -- never a
production app secret.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from abc import ABC, abstractmethod

from .adapter import MessagingAdapter, OutboundMessage

logger = logging.getLogger("chambeando.whatsapp.meta_client")


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


class MetaApiError(Exception):
    """Raised on any failure sending through the real Meta Cloud API --
    network error, timeout, or a non-2xx response. Callers (currently just
    WhatsAppAdapter.send) are expected to let this propagate; this phase
    does not implement automatic retry (see class docstring below)."""


class MetaCloudWhatsAppClient(MetaClient):
    """Real Meta WhatsApp Cloud API transport (Phase 2D.1). Configuration
    comes ONLY from constructor arguments (which `messaging/__init__.py`
    populates from `settings.WHATSAPP_*`, i.e. environment variables) --
    never a hardcoded credential, never persisted to the database, never
    logged (see send_message's error path).

    No automatic retry: sending a WhatsApp message is NOT an idempotent
    operation from Meta's side (no client-supplied dedupe key is used here),
    so blindly retrying a request that may have already succeeded on Meta's
    end risks a duplicate message to a real person -- worse than a single
    failed send. A caller that wants retry semantics must implement its own
    idempotency (e.g. checking message history) before retrying."""

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        graph_api_version: str = "v21.0",
        http_client=None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not access_token or not phone_number_id:
            raise ValueError("MetaCloudWhatsAppClient requires a non-empty access_token and phone_number_id")
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._base_url = f"https://graph.facebook.com/{graph_api_version}"
        self._timeout_seconds = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=self._timeout_seconds)
        return self._http

    def send_message(self, to: str, text: str) -> None:
        import httpx

        url = f"{self._base_url}/{self._phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}

        try:
            response = self._client().post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            logger.error("WhatsApp send failed: request timed out")
            raise MetaApiError("timeout sending WhatsApp message") from exc
        except httpx.RequestError as exc:
            # never log str(exc) verbatim -- httpx request errors can embed
            # the request URL (which itself never contains the token, but
            # this stays defensive: log the exception TYPE, not its message)
            logger.error("WhatsApp send failed: network error (%s)", type(exc).__name__)
            raise MetaApiError("network error sending WhatsApp message") from exc

        if response.status_code >= 300:
            error_code = None
            try:
                error_code = response.json().get("error", {}).get("code")
            except ValueError:
                pass  # non-JSON error body -- still never logged verbatim below
            # sanitized: status code + Meta's own numeric error code only --
            # never response.text (could echo request content back) and
            # never any request header (the Authorization bearer token lives
            # only in `headers` above, never passed to this logger call).
            logger.error("WhatsApp send failed: status=%s error_code=%s", response.status_code, error_code)
            raise MetaApiError(f"Meta API returned HTTP {response.status_code}")

    def close(self) -> None:
        if self._owns_client and self._http is not None:
            self._http.close()


class WhatsAppAdapter(MessagingAdapter):
    def __init__(self, client: MetaClient | None = None) -> None:
        self._client = client or SandboxMetaClient()

    def send(self, message: OutboundMessage) -> None:
        text = message.text
        if message.action_url:
            text = f"{text}\n{message.action_url}"
        try:
            self._client.send_message(message.to, text)
        except MetaApiError:
            # Best-effort delivery: MessagingAdapter's contract (adapter.py)
            # is channel-agnostic and callers (ConversationRouter) must never
            # see a Meta-specific exception -- a failed reply is swallowed
            # here, not propagated up into the inbound webhook request that
            # triggered it (which must still ack Meta with 2xx regardless).
            # MetaClient.send_message already logged the sanitized status/
            # error_code; nothing more to add except accepting the lost reply.
            logger.error("WhatsApp outbound send failed; reply not delivered")


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
