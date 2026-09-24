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
    """Raised on any failure calling the real Telegram Bot API -- network
    error, timeout, or a non-2xx response."""


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
            logger.error("Telegram send failed: request timed out")
            raise TelegramApiError("timeout sending Telegram message") from exc
        except httpx.RequestError as exc:
            logger.error("Telegram send failed: network error (%s)", type(exc).__name__)
            raise TelegramApiError("network error sending Telegram message") from exc

        if response.status_code >= 300:
            error_code = None
            try:
                error_code = response.json().get("error_code")
            except ValueError:
                pass
            # sanitized: status + Telegram's own numeric error code only --
            # never response.text and never the bot_token (lives only in
            # self._base_url, never passed to this logger call).
            logger.error("Telegram send failed: status=%s error_code=%s", response.status_code, error_code)
            raise TelegramApiError(f"Telegram API returned HTTP {response.status_code}")

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
