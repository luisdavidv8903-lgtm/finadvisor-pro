"""
Channel-agnostic messaging port (Phase 2C section 12). Business/conversation
code (router.py, notifications.py) depends ONLY on this interface, never on
WhatsApp/Meta-specific request shapes -- the same separation principle as
EscrowChainAdapter for the chain. A future Telegram/web-chat channel means a
new MessagingAdapter implementation, not a rewrite of the conversation logic.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class OutboundMessage:
    """`to` is an opaque, channel-specific recipient id (e.g. a WhatsApp id) --
    never a raw phone number requirement, never logged together with `text`
    (see security/audit-style "never log the sensitive value" discipline,
    applied here to message content)."""

    to: str
    text: str
    action_url: str | None = None  # optional secure deep-link (wallet signing, settlement/evidence handoff)


class MessagingAdapter(ABC):
    @abstractmethod
    def send(self, message: OutboundMessage) -> None: ...
