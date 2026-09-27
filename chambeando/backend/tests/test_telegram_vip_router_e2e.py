"""
End-to-end tests for the Telegram-as-source-of-truth VIP pilot (2026-09-27),
through the REAL HTTP webhook route (POST /telegram/webhook) -- same level
as test_whatsapp_vip_router_e2e.py, but exercising telegram_vip_router.py.

Covers the pilot's explicit small-test scope: two synthetic Telegram
identities (one creates an offer, the other opens the generated deep link
and expresses interest), plus expiration, resend/duplicate, and a
tampered/altered deep link -- and a sanity check that the pre-existing open
marketplace (TelegramConversationRouter) is still reachable unchanged
through the same webhook route after this revision's routing split.

NEVER claims this was validated against real Telegram accounts or a real
deployment -- every request here goes through TestClient against a
SandboxTelegramClient, never the real Bot API, and TELEGRAM_VIP_BOT_USERNAME
is a synthetic value set only for this test file.
"""
import pytest

from backend.config import settings
from backend.messaging import TelegramVipRouter, reset_telegram_vip_router_for_tests, set_telegram_vip_router_for_tests
from backend.messaging.telegram_adapter import SandboxTelegramClient
from backend.messaging.telegram_vip_router import build_offer_deep_link, validate_telegram_bot_username
from backend.messaging.whatsapp_vip_identity import create_vip_invite
from backend.models import WhatsAppVipOfferDB, WhatsAppVipOfferInterestDB
from backend.security.rate_limit import reset_rate_limiter_for_tests

_SYNTHETIC_BOT_USERNAME = "chambeando_vip_pilot_bot"


@pytest.fixture()
def telegram_vip_sandbox(db_session, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_VIP_BOT_USERNAME", _SYNTHETIC_BOT_USERNAME)
    telegram_client = SandboxTelegramClient()
    set_telegram_vip_router_for_tests(TelegramVipRouter(telegram_client))
    reset_rate_limiter_for_tests()
    yield telegram_client
    reset_telegram_vip_router_for_tests()


def _send(client, chat_id: str, text: str, msg_id: str):
    update = {
        "update_id": abs(hash(msg_id)) % 1_000_000,
        "message": {"message_id": msg_id, "from": {"id": chat_id, "is_bot": False}, "chat": {"id": chat_id, "type": "private"}, "text": text},
    }
    return client.post(
        "/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": settings.TELEGRAM_WEBHOOK_SECRET},
    )


def _last_text_to(sandbox: SandboxTelegramClient, chat_id: str) -> str:
    for to, text in reversed(sandbox.sent):
        if to == chat_id:
            return text
    return ""


def _create_full_offer(client, sandbox, db_session, chat_id: str, *, msg_prefix: str, give="2", receive="1", give_method="1", receive_method="3") -> int:
    """Drives the whole Telegram VIP wizard for one identity, starting from
    invite redemption -- the SAME way a real invited Telegram member would
    gain access (requirement 2: 'acceso solo a miembros invitados'), never
    a test-only shortcut. Unlike the WhatsApp E2E suite (which has
    authorize_whatsapp_id, a direct-authorize admin path), there is no
    Telegram equivalent by design -- see whatsapp_vip_identity.py -- so
    every test in this file redeems a real invite code first.

    Menu indices match test_whatsapp_vip_router_e2e.py's _create_full_offer
    exactly (same shared menu-building helpers, imported from
    whatsapp_vip_router.py) -- currency order USD,CUP,EUR,MLC,MXN (give=2
    -> CUP; after excluding CUP, receive=1 -> USD). Returns the new offer's
    id."""
    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    r_redeem = _send(client, chat_id, f"VIP {code}", f"{msg_prefix}.redeem")
    assert r_redeem.status_code == 200, r_redeem.text
    assert "autorizado" in _last_text_to(sandbox, chat_id).lower()

    steps = [
        ("OFERTA", "start"),
        ("1", "intent"),  # COMPRO
        (give, "give_currency"),
        (receive, "receive_currency"),
        ("4000", "amount"),
        ("1", "rate_unit"),
        ("445", "rate_value"),
        (give_method, "give_method"),
        ("La Habana", "give_location"),
        (receive_method, "receive_method"),
        ("Miami", "receive_location"),
        ("1", "duration"),  # 24h
        ("CONFIRMAR", "confirm"),
    ]
    for i, (text, _label) in enumerate(steps):
        r = _send(client, chat_id, text, f"{msg_prefix}.{i}")
        assert r.status_code == 200, (i, text, r.text)

    confirm_text = _last_text_to(sandbox, chat_id)
    assert "publicada" in confirm_text.lower()
    offer = db_session.query(WhatsAppVipOfferDB).order_by(WhatsAppVipOfferDB.id.desc()).first()
    return offer.id


def test_creator_and_interested_party_via_deep_link(client, telegram_vip_sandbox, db_session):
    """THE point-6 scenario: identity A creates an offer through the private
    Telegram wizard; the confirmation includes a copy-paste WhatsApp text
    AND a t.me deep link (requirement 3). Identity B -- a completely
    separate synthetic Telegram identity -- opens that link (/start
    offer_<id>), sees the offer, and expresses interest; A is notified
    without B's Telegram id ever appearing in that notification
    (requirement 5)."""
    sandbox = telegram_vip_sandbox
    creator = "tg-vip-creator-1"
    interested = "tg-vip-interested-1"

    offer_id = _create_full_offer(client, sandbox, db_session, creator, msg_prefix="tgvip.create")
    confirm_text = _last_text_to(sandbox, creator)
    expected_link = f"https://t.me/{_SYNTHETIC_BOT_USERNAME}?start=offer_{offer_id}"
    assert expected_link in confirm_text
    assert "grupo vip de whatsapp" in confirm_text.lower()

    # Second identity redeems its OWN invite (independent membership), then
    # taps the deep link -- exactly the text Telegram would send.
    code_b = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    r_redeem_b = _send(client, interested, f"VIP {code_b}", "tgvip.b.redeem")
    assert r_redeem_b.status_code == 200
    assert "autorizado" in _last_text_to(sandbox, interested).lower()

    r_open = _send(client, interested, f"/start offer_{offer_id}", "tgvip.b.open")
    assert r_open.status_code == 200
    offer_view = _last_text_to(sandbox, interested)
    assert f"#{offer_id}" in offer_view
    assert "me interesa" in offer_view.lower()
    assert creator not in offer_view  # never leak the owner's own telegram id either

    r_interest = _send(client, interested, f"ME INTERESA {offer_id}", "tgvip.b.interest")
    assert r_interest.status_code == 200
    assert "interes registrado" in _last_text_to(sandbox, interested).lower()

    owner_notification = _last_text_to(sandbox, creator)
    assert "interes" in owner_notification.lower()
    assert interested not in owner_notification  # never leak the interested party's telegram id to the owner

    assert db_session.query(WhatsAppVipOfferInterestDB).count() == 1


def test_resend_same_me_interesa_is_idempotent_no_second_notification(client, telegram_vip_sandbox, db_session):
    """'reenvío'/'duplicados' (requirement 6): the SAME identity sending
    ME INTERESA twice must register exactly once and notify the owner
    exactly once, never twice."""
    sandbox = telegram_vip_sandbox
    creator = "tg-vip-resend-owner"
    interested = "tg-vip-resend-person"

    offer_id = _create_full_offer(client, sandbox, db_session, creator, msg_prefix="tgvip.resend")
    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    _send(client, interested, f"VIP {code}", "tgvip.resend.b.redeem")

    r1 = _send(client, interested, f"ME INTERESA {offer_id}", "tgvip.resend.interest1")
    assert r1.status_code == 200
    sent_count_after_first = len(sandbox.sent)

    r2 = _send(client, interested, f"ME INTERESA {offer_id}", "tgvip.resend.interest2")
    assert r2.status_code == 200
    assert "ya habias mostrado interes" in _last_text_to(sandbox, interested).lower()

    owner_notifications = [t for to, t in sandbox.sent if to == creator and "mostro interes" in t.lower()]
    assert len(owner_notifications) == 1, "a resent ME INTERESA must never notify the owner a second time"
    assert len(sandbox.sent) == sent_count_after_first + 1  # only the "ya habias mostrado interes" reply
    assert db_session.query(WhatsAppVipOfferInterestDB).count() == 1


def test_webhook_redelivery_of_same_message_id_does_not_duplicate_offer(client, telegram_vip_sandbox, db_session):
    """'duplicados' (requirement 6), from the webhook-delivery angle: Telegram
    (or a proxy in front of it) redelivering the exact same update
    (identical message_id) must be a pure no-op -- covered here by
    ProcessedWebhookEventDB's unique constraint, exactly like the WhatsApp
    VIP flow's own idempotency marker."""
    sandbox = telegram_vip_sandbox
    creator = "tg-vip-dupe-owner"

    _create_full_offer(client, sandbox, db_session, creator, msg_prefix="tgvip.dupe")
    assert db_session.query(WhatsAppVipOfferDB).count() == 1
    confirm_message_id = "tgvip.dupe.12"  # CONFIRMAR is step index 12 in _create_full_offer's steps list

    # Redeliver the EXACT same final CONFIRMAR message_id.
    r_redelivered = _send(client, creator, "CONFIRMAR", confirm_message_id)
    assert r_redelivered.status_code == 200
    assert r_redelivered.json()["duplicates"] == 1

    offers = db_session.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.source_message_id == confirm_message_id).all()
    assert len(offers) == 1, "a redelivered webhook update must never create a duplicate offer"


def test_expired_offer_shown_as_such_via_deep_link_and_interest_is_blocked(client, telegram_vip_sandbox, db_session):
    """Requirement 4: opening the deep link for an offer that already
    expired must show that state, never the live offer details, and
    ME INTERESA on it must never succeed."""
    from datetime import timedelta

    from backend import timeutils

    sandbox = telegram_vip_sandbox
    creator = "tg-vip-expiry-owner"
    viewer = "tg-vip-expiry-viewer"

    offer_id = _create_full_offer(client, sandbox, db_session, creator, msg_prefix="tgvip.expiry")
    offer = db_session.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.id == offer_id).first()
    offer.expires_at = timeutils.utcnow() - timedelta(hours=1)
    db_session.commit()

    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    _send(client, viewer, f"VIP {code}", "tgvip.expiry.viewer.redeem")

    r_open = _send(client, viewer, f"/start offer_{offer_id}", "tgvip.expiry.open")
    assert r_open.status_code == 200
    view_text = _last_text_to(sandbox, viewer)
    assert "expired" in view_text.lower() or "ya no esta disponible" in view_text.lower()
    assert "me interesa" not in view_text.lower()

    r_interest = _send(client, viewer, f"ME INTERESA {offer_id}", "tgvip.expiry.interest")
    assert r_interest.status_code == 200
    assert "no se pudo registrar el interes" in _last_text_to(sandbox, viewer).lower()
    assert db_session.query(WhatsAppVipOfferInterestDB).count() == 0


@pytest.mark.parametrize(
    "tampered_text",
    [
        "/start offer_notanumber",  # non-numeric payload
        "/start offer_999999999",  # well-formed but non-existent offer id
        "/start",  # bare start, no payload at all (handled by the OTHER router -- see below)
    ],
)
def test_altered_deep_link_never_crashes_and_never_allows_interest(client, telegram_vip_sandbox, db_session, tampered_text):
    """'enlace alterado' (requirement 6): a tampered or garbled deep link
    must always get a 200 (never crash the webhook), and must never let
    the tamperer register interest in anything."""
    sandbox = telegram_vip_sandbox
    stranger = f"tg-vip-tamper-{abs(hash(tampered_text))}"

    r = _send(client, stranger, tampered_text, f"tgvip.tamper.{abs(hash(tampered_text))}")
    assert r.status_code == 200, r.text
    assert db_session.query(WhatsAppVipOfferInterestDB).count() == 0
    assert db_session.query(WhatsAppVipOfferDB).count() == 0


def test_unauthorized_identity_denied_for_every_vip_command(client, telegram_vip_sandbox, db_session):
    """Requirement 2: access is invite-only, same rule as the WhatsApp VIP
    pilot -- a Telegram identity that never redeemed a code must be denied,
    even though telegram_identity.py's OWN open-marketplace rule (any /start
    grants access) does not apply here at all."""
    sandbox = telegram_vip_sandbox
    stranger = "tg-vip-unauthorized-1"

    for i, cmd in enumerate(["OFERTA", "OFERTAS", "MIS OFERTAS", "ME INTERESA 1", "CERRAR 1"]):
        r = _send(client, stranger, cmd, f"tgvip.unauth.{i}")
        assert r.status_code == 200
        assert "no estas autorizado" in _last_text_to(sandbox, stranger).lower()

    assert db_session.query(WhatsAppVipOfferDB).count() == 0


def test_bot_username_must_be_configured_and_valid_before_a_deep_link_is_built(monkeypatch):
    """Requirement 3: 'No inventes el username: configuralo y validalo.'
    Never fabricates a link from an unset or malformed username."""
    monkeypatch.setattr(settings, "TELEGRAM_VIP_BOT_USERNAME", None)
    assert validate_telegram_bot_username(None) is None
    assert build_offer_deep_link(1) is None

    monkeypatch.setattr(settings, "TELEGRAM_VIP_BOT_USERNAME", "no")  # too short, doesn't end in "bot"
    assert validate_telegram_bot_username("no") is None

    monkeypatch.setattr(settings, "TELEGRAM_VIP_BOT_USERNAME", "not a valid username!")
    assert validate_telegram_bot_username("not a valid username!") is None

    monkeypatch.setattr(settings, "TELEGRAM_VIP_BOT_USERNAME", _SYNTHETIC_BOT_USERNAME)
    assert validate_telegram_bot_username(_SYNTHETIC_BOT_USERNAME) == _SYNTHETIC_BOT_USERNAME
    assert build_offer_deep_link(42) == f"https://t.me/{_SYNTHETIC_BOT_USERNAME}?start=offer_42"


def test_offer_confirmation_shows_placeholder_text_never_a_fabricated_link_when_unconfigured(client, db_session, monkeypatch):
    """Same requirement, exercised end to end: with TELEGRAM_VIP_BOT_USERNAME
    unset, the wizard still completes (the offer is real) but the
    confirmation text says the link is unavailable instead of guessing one."""
    monkeypatch.setattr(settings, "TELEGRAM_VIP_BOT_USERNAME", None)
    telegram_client = SandboxTelegramClient()
    set_telegram_vip_router_for_tests(TelegramVipRouter(telegram_client))
    reset_rate_limiter_for_tests()
    try:
        creator = "tg-vip-nolink-owner"
        _create_full_offer(client, telegram_client, db_session, creator, msg_prefix="tgvip.nolink")
        confirm_text = _last_text_to(telegram_client, creator)
        assert "t.me/" not in confirm_text
        assert "no disponible" in confirm_text.lower()
    finally:
        reset_telegram_vip_router_for_tests()


def test_legacy_open_marketplace_still_reachable_through_the_real_webhook(client, telegram_vip_sandbox, db_session):
    """Sanity check for the routers/telegram.py routing split this revision
    introduced: a plain '/START' (no offer_<id> payload) must still reach
    the pre-existing TelegramConversationRouter, completely untouched --
    never intercepted by the new VIP routing."""
    from backend.messaging import TelegramConversationRouter, TelegramAdapter as _TelegramAdapter, reset_telegram_router_for_tests, set_telegram_router_for_tests
    from backend.messaging.telegram_adapter import SandboxTelegramClient as _SandboxTelegramClient

    marketplace_client = _SandboxTelegramClient()
    set_telegram_router_for_tests(TelegramConversationRouter(_TelegramAdapter(marketplace_client)))
    try:
        r = _send(client, "tg-marketplace-untouched-1", "/START", "tgvip.marketplace.start")
        assert r.status_code == 200
        assert any(to == "tg-marketplace-untouched-1" for to, _ in marketplace_client.sent)
        assert not any(to == "tg-marketplace-untouched-1" for to, _ in telegram_vip_sandbox.sent)
        marketplace_texts = " ".join(t for to, t in marketplace_client.sent if to == "tg-marketplace-untouched-1")
        assert "bienvenido" in marketplace_texts.lower()
        assert "menu principal" in marketplace_texts.lower()
    finally:
        reset_telegram_router_for_tests()
