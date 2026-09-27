"""
Telegram implementation of MessagingAdapter -- same SandboxClient/real-client
split as whatsapp_adapter.py, behind the same MessagingAdapter interface, so
ConversationRouter-style business code never depends on transport specifics.

Webhook security: Telegram has no HMAC signature scheme like Meta's
X-Hub-Signature-256. Instead it supports an operator-chosen secret token
(set via setWebhook's secret_token param) echoed back on every delivery as
the `X-Telegram-Bot-Api-Secret-Token` header -- verify_webhook_secret below
does a constant-time compare, same discipline as Meta's verify functions.
"""
from __future__ import annotations

import hmac
import logging
from abc import ABC, abstractmethod

from .adapter import MessagingAdapter, OutboundMessage

logger = logging.getLogger("chambeando.telegram.client")


class TelegramClient(ABC):
    @abstractmethod
    def send_message(self, chat_id: str, text: str) -> None: ...


class SandboxTelegramClient(TelegramClient):
    """In-memory fake -- never makes an HTTP call. Same role as
    SandboxMetaClient/FakeChainAdapter elsewhere in this codebase."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class TelegramApiError(Exception):
    """Base class for every failure calling the real Telegram Bot API.
    Never raised directly by RealTelegramClient itself -- every call to
    send_message raises exactly one of the three subclasses below, each
    carrying enough SANITIZED diagnostic detail (status_code, Telegram's
    own numeric error_code) for a caller to classify the outcome without
    re-inspecting a raw response. NEVER carries the raw response body/text
    or the bot_token (that stays only in self._base_url, never attached to
    an exception or a log call)."""

    def __init__(self, message: str, *, status_code: int | None = None, error_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class TelegramApiUncertainError(TelegramApiError):
    """Raised when we never received a definitive response at all -- a
    request timeout, or the connection was cut/reset/lost mid-request or
    mid-response (2026-09-27 audit, "cierra dos riesgos"). Telegram's
    server may already have received and processed the sendMessage call
    before the failure happened on OUR end; there is no way to know from
    here. Deliberately conservative: this covers httpx.TimeoutException AND
    the broader httpx.RequestError (which includes connection failures) --
    no attempt is made to further distinguish "definitely never reached
    Telegram" (e.g. a bare connect failure) from "reached it, response
    lost" (a read timeout after the request was sent), because getting
    that distinction wrong in the unsafe direction risks a real duplicate
    message to a real person, which this whole mechanism exists to avoid.
    status_code/error_code are always None here -- no response was ever
    received to read them from.

    Subclasses TelegramApiError (so any existing `except TelegramApiError`,
    e.g. TelegramAdapter.send's best-effort swallow, still catches it) --
    but services/telegram_outbox.py's attempt_send_one checks for THIS
    specific type FIRST and routes it straight to
    UNCERTAIN_NEEDS_RECONCILIATION, never FAILED_RETRYABLE: a blind retry
    here is exactly the "reintento ciego de un envio incierto" this
    exception type exists to prevent."""


class TelegramApiPermanentError(TelegramApiError):
    """Raised when a response WAS received and it is a DEFINITIVE,
    known-permanent rejection -- HTTP 400 (malformed request), 401
    (invalid bot token), 403 (bot blocked/kicked from the chat), or any
    other 4xx status except 429. These are explicit, reviewed classifications,
    not a blanket "all 4xx = permanent" assumption applied without looking
    (2026-09-27 audit, "no supongas que todos los codigos de un rango
    tienen la misma causa"): 429 is deliberately excluded from this bucket
    even though it is a 4xx, because it means something categorically
    different (a rate limit that clears with time, not a request that will
    never succeed) -- see TelegramApiTransientError. The reasoning for
    treating every OTHER 4xx as permanent is ordinary HTTP semantics: a
    client error means THIS EXACT request is malformed or unauthorized for
    THIS EXACT chat -- retrying the identical request changes nothing, so
    services/telegram_outbox.py's attempt_send_one never retries this,
    ever, and moves straight to FAILED_PERMANENT with an operational
    signal (a dedicated log line + SecurityEventType) instead."""


class TelegramApiTransientError(TelegramApiError):
    """Raised when a response WAS received and it indicates a TRANSIENT,
    possibly-self-clearing condition -- HTTP 429 (rate limited) or any 5xx
    (server-side error). Safe to retry, bounded by the same
    WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS budget every other retryable failure
    uses (never an infinite loop). `retry_after` (seconds), when Telegram's
    response supplies one (its JSON body's `parameters.retry_after`, or a
    standard HTTP `Retry-After` header), is honored by
    services/telegram_outbox.py's attempt_send_one as the MINIMUM backoff
    for the next attempt -- never shorter than what Telegram itself asked
    for, and always capped (see _backoff_seconds) so a single very large
    value can never stall an operator's ability to reconcile the outbox in
    a reasonable window."""

    def __init__(self, message: str, *, status_code: int | None = None, error_code: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message, status_code=status_code, error_code=error_code)
        self.retry_after = retry_after


class RealTelegramClient(TelegramClient):
    def __init__(self, *, bot_token: str, http_client=None, timeout_seconds: float = 10.0) -> None:
        if not bot_token:
            raise ValueError("RealTelegramClient requires a non-empty bot_token")
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._timeout_seconds = timeout_seconds
        self._owns_client = http_client is None
        self._http = http_client

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=self._timeout_seconds)
        return self._http

    def send_message(self, chat_id: str, text: str) -> None:
        import httpx

        url = f"{self._base_url}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}

        try:
            response = self._client().post(url, json=payload)
        except httpx.TimeoutException as exc:
            logger.error("Telegram send failed: request timed out -- outcome uncertain, Telegram may have already received it")
            raise TelegramApiUncertainError("timeout sending Telegram message") from exc
        except httpx.RequestError as exc:
            logger.error("Telegram send failed: network error (%s) -- outcome uncertain", type(exc).__name__)
            raise TelegramApiUncertainError("network error sending Telegram message") from exc

        if response.status_code >= 300:
            status = response.status_code
            error_code = None
            retry_after = None
            try:
                body = response.json()
                error_code = body.get("error_code")
                parameters = body.get("parameters")
                if isinstance(parameters, dict):
                    retry_after = parameters.get("retry_after")
            except ValueError:
                pass
            if retry_after is None:
                header_value = response.headers.get("Retry-After")
                if header_value is not None:
                    try:
                        retry_after = int(header_value)
                    except ValueError:
                        retry_after = None

            # sanitized: status + Telegram's own numeric error code +
            # retry_after only -- never response.text and never the
            # bot_token (lives only in self._base_url, never passed to
            # this logger call).
            logger.error("Telegram send failed: status=%s error_code=%s retry_after=%s", status, error_code, retry_after)

            # 429 and every 5xx are transient (self-clearing, safe to
            # retry); every OTHER non-2xx is treated as a permanent
            # rejection of this exact request -- see each exception
            # class's own docstring for why this is a reviewed
            # classification, not a blind range assumption.
            if status == 429 or 500 <= status < 600:
                raise TelegramApiTransientError(f"Telegram API returned HTTP {status}", status_code=status, error_code=error_code, retry_after=retry_after)
            raise TelegramApiPermanentError(f"Telegram API returned HTTP {status}", status_code=status, error_code=error_code)

    def close(self) -> None:
        if self._owns_client and self._http is not None:
            self._http.close()


class TelegramAdapter(MessagingAdapter):
    def __init__(self, client: TelegramClient | None = None) -> None:
        self._client = client or SandboxTelegramClient()

    def send(self, message: OutboundMessage) -> None:
        text = message.text
        if message.action_url:
            text = f"{text}\n{message.action_url}"
        try:
            self._client.send_message(message.to, text)
        except TelegramApiError:
            # Best-effort delivery, same contract as WhatsAppAdapter.send: a
            # failed reply must never propagate into the inbound webhook
            # request that triggered it (which must still ack Telegram with
            # 2xx regardless).
            logger.error("Telegram outbound send failed; reply not delivered")


def verify_webhook_secret(secret_header: str | None, expected_secret: str) -> bool:
    return secret_header is not None and hmac.compare_digest(secret_header, expected_secret)
