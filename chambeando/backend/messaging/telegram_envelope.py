"""
Telegram Bot API `Update` envelope parsing -- the Telegram counterpart of
meta_envelope.py. Handles only what Chambeando V1 needs (private-chat text
messages); everything else (edited_message, channel_post, callback_query,
non-text content) is safely ignored, never a parse error.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedInboundMessage:
    message_id: str
    channel_user_id: str
    text: str


def parse_telegram_update(raw: dict) -> NormalizedInboundMessage | None:
    """None means "not something Chambeando V1 handles" (e.g. a
    callback_query, an edited_message, a non-text message) -- caller
    acknowledges (200) and skips, never raises."""
    message = raw.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not text:
        return None
    from_user = message.get("from") or {}
    user_id = from_user.get("id")
    message_id = message.get("message_id")
    if user_id is None or message_id is None:
        return None
    return NormalizedInboundMessage(message_id=str(message_id), channel_user_id=str(user_id), text=text)
