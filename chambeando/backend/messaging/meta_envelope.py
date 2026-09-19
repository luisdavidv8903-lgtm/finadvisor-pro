"""
Real Meta WhatsApp Cloud API webhook envelope (Phase 2D.1) -- replaces the
simplified synthetic `WhatsAppWebhookPayload` shape from Phase 2C. Models
only the fields Chambeando actually needs (Phase 2D.1 section 3: "Extract
only minimal fields needed by ConversationRouter"), with `extra="ignore"`
everywhere so unknown/future Meta fields never break parsing.

Structure (per Meta's Cloud API docs):
    {"object": "whatsapp_business_account", "entry": [
        {"id": "<WABA id>", "changes": [
            {"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "...", "display_phone_number": "..."},
                "contacts": [{"wa_id": "...", "profile": {"name": "..."}}],
                "messages": [{"id": "wamid...", "from": "...", "type": "text", "text": {"body": "..."}}],
                "statuses": [{"id": "wamid...", "status": "delivered", "recipient_id": "..."}]
            }}
        ]}
    ]}

`messages` and `statuses` are DIFFERENT things carried in the SAME envelope
-- a status update (sent/delivered/read/failed, about a message WE sent) is
never a user's inbound message, and parse_meta_webhook_envelope() below
never converts one into the other (Phase 2D.1 section 6: "Status webhooks
must not be mistaken for inbound user messages").
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class MetaTextBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    body: str = ""


class MetaButtonReply(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    title: str = ""


class MetaInteractive(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str = ""
    button_reply: MetaButtonReply | None = None


class MetaQuickReplyButton(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = ""
    payload: str = ""


class MetaMessage(BaseModel):
    """One inbound message. `type` decides which of text/interactive/button
    is populated; anything else (image, audio, video, document, location,
    sticker, contacts, unknown/future types) is safely ignored -- see
    _extract_text below, never a crash."""

    model_config = ConfigDict(extra="ignore")
    id: str
    from_wa_id: str = Field(alias="from")
    timestamp: str | None = None
    type: str = ""
    text: MetaTextBody | None = None
    interactive: MetaInteractive | None = None
    button: MetaQuickReplyButton | None = None


class MetaStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    status: str = ""
    recipient_id: str | None = None


class MetaMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    phone_number_id: str | None = None
    display_phone_number: str | None = None


class MetaValue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    messaging_product: str | None = None
    metadata: MetaMetadata | None = None
    messages: list[MetaMessage] = Field(default_factory=list)
    statuses: list[MetaStatus] = Field(default_factory=list)


class MetaChange(BaseModel):
    model_config = ConfigDict(extra="ignore")
    field: str | None = None
    value: MetaValue = Field(default_factory=MetaValue)


class MetaEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    changes: list[MetaChange] = Field(default_factory=list)


class MetaWebhookEnvelope(BaseModel):
    """The full POST body Meta sends. `entry`/`changes` are always lists --
    Meta batches multiple entries/changes into a single webhook delivery,
    which this envelope (and the parser below) handles natively, not as a
    special case."""

    model_config = ConfigDict(extra="ignore")
    object: str | None = None
    entry: list[MetaEntry] = Field(default_factory=list)


@dataclass(frozen=True)
class NormalizedInboundMessage:
    """What ConversationRouter actually needs -- nothing more."""

    message_id: str
    whatsapp_id: str
    text: str


def _extract_text(message: MetaMessage) -> str | None:
    """None means "not a type Chambeando handles yet" -- caller skips it,
    never raises. This is the ONLY place that decides which Meta message
    types map to conversational text input."""
    if message.type == "text" and message.text is not None:
        return message.text.body
    if message.type == "interactive" and message.interactive is not None and message.interactive.button_reply is not None:
        return message.interactive.button_reply.id or message.interactive.button_reply.title
    if message.type == "button" and message.button is not None:
        return message.button.payload or message.button.text
    return None


def parse_meta_webhook_envelope(envelope: MetaWebhookEnvelope) -> list[NormalizedInboundMessage]:
    """Flattens every entry/change/message in the envelope into normalized
    inbound messages, safely skipping `statuses` entirely and any message
    type _extract_text doesn't recognize. Never raises on an unexpected
    shape within an individual message -- unsupported content is acknowledged
    (by being skipped) rather than crashing the whole webhook delivery."""
    normalized: list[NormalizedInboundMessage] = []
    for entry in envelope.entry:
        for change in entry.changes:
            for message in change.value.messages:
                text = _extract_text(message)
                if text is None:
                    continue
                normalized.append(NormalizedInboundMessage(message_id=message.id, whatsapp_id=message.from_wa_id, text=text))
            # change.value.statuses are deliberately never converted to inbound messages
    return normalized
