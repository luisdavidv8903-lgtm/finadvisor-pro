"""
Phase 2D.1 — unit tests for the real Meta webhook envelope parser
(messaging/meta_envelope.py), independent of any HTTP route.
"""
from backend.messaging.meta_envelope import MetaWebhookEnvelope, parse_meta_webhook_envelope


def test_parses_plain_text_message():
    envelope = MetaWebhookEnvelope.model_validate(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_1",
                    "changes": [
                        {"field": "messages", "value": {"messages": [{"id": "wamid.1", "from": "wa-1", "type": "text", "text": {"body": "hola"}}]}}
                    ],
                }
            ],
        }
    )
    normalized = parse_meta_webhook_envelope(envelope)
    assert len(normalized) == 1
    assert normalized[0].message_id == "wamid.1"
    assert normalized[0].whatsapp_id == "wa-1"
    assert normalized[0].text == "hola"


def test_parses_interactive_button_reply_using_id():
    envelope = MetaWebhookEnvelope.model_validate(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.2",
                                        "from": "wa-2",
                                        "type": "interactive",
                                        "interactive": {"type": "button_reply", "button_reply": {"id": "BUY", "title": "Buy now"}},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )
    normalized = parse_meta_webhook_envelope(envelope)
    assert normalized[0].text == "BUY"


def test_parses_quick_reply_button_using_payload_over_text():
    envelope = MetaWebhookEnvelope.model_validate(
        {"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.3", "from": "wa-3", "type": "button", "button": {"text": "Sell", "payload": "SELL"}}]}}]}]}
    )
    normalized = parse_meta_webhook_envelope(envelope)
    assert normalized[0].text == "SELL"


def test_multiple_entries_and_changes_all_flattened():
    envelope = MetaWebhookEnvelope.model_validate(
        {
            "entry": [
                {"changes": [{"value": {"messages": [{"id": "wamid.a", "from": "wa-a", "type": "text", "text": {"body": "1"}}]}}]},
                {
                    "changes": [
                        {"value": {"messages": [{"id": "wamid.b", "from": "wa-b", "type": "text", "text": {"body": "2"}}]}},
                        {"value": {"messages": [{"id": "wamid.c", "from": "wa-c", "type": "text", "text": {"body": "3"}}]}},
                    ]
                },
            ]
        }
    )
    normalized = parse_meta_webhook_envelope(envelope)
    assert {n.whatsapp_id for n in normalized} == {"wa-a", "wa-b", "wa-c"}


def test_statuses_never_become_inbound_messages():
    envelope = MetaWebhookEnvelope.model_validate(
        {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.status1", "status": "delivered", "recipient_id": "wa-1"}]}}]}]}
    )
    assert parse_meta_webhook_envelope(envelope) == []


def test_unsupported_message_type_safely_skipped_not_crashed():
    envelope = MetaWebhookEnvelope.model_validate(
        {"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.img1", "from": "wa-1", "type": "image", "image": {"id": "media-123"}}]}}]}]}
    )
    assert parse_meta_webhook_envelope(envelope) == []


def test_mixed_supported_and_unsupported_in_same_delivery():
    envelope = MetaWebhookEnvelope.model_validate(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"id": "wamid.ok", "from": "wa-1", "type": "text", "text": {"body": "hi"}},
                                    {"id": "wamid.bad", "from": "wa-2", "type": "sticker", "sticker": {"id": "s1"}},
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )
    normalized = parse_meta_webhook_envelope(envelope)
    assert len(normalized) == 1
    assert normalized[0].message_id == "wamid.ok"


def test_empty_envelope_returns_empty_list():
    assert parse_meta_webhook_envelope(MetaWebhookEnvelope.model_validate({"object": "whatsapp_business_account", "entry": []})) == []


def test_unknown_extra_fields_never_break_parsing():
    """Meta may add fields Chambeando doesn't model yet -- extra="ignore"
    everywhere means a real, evolving payload never crashes the parser."""
    envelope = MetaWebhookEnvelope.model_validate(
        {
            "object": "whatsapp_business_account",
            "some_future_top_level_field": {"nested": True},
            "entry": [
                {
                    "id": "WABA_1",
                    "some_future_entry_field": 123,
                    "changes": [
                        {
                            "field": "messages",
                            "some_future_change_field": "x",
                            "value": {
                                "messaging_product": "whatsapp",
                                "some_future_value_field": [1, 2, 3],
                                "messages": [
                                    {
                                        "id": "wamid.future1",
                                        "from": "wa-1",
                                        "type": "text",
                                        "text": {"body": "hi", "some_future_text_field": "x"},
                                        "some_future_message_field": True,
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    )
    normalized = parse_meta_webhook_envelope(envelope)
    assert normalized[0].text == "hi"
