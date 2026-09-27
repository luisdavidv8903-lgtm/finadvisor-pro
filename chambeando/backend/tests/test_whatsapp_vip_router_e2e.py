"""
End-to-end WhatsApp VIP offer-board tests, through the REAL HTTP webhook
route (POST /whatsapp/webhook) -- same level as test_whatsapp_webhook_security.py,
but exercising whatsapp_vip_router.py instead of the wallet-flow
ConversationRouter. Covers: two identities creating offers in opposite
directions, browse/interest/close, unauthorized denial (requirement 1),
VIP <code> redemption, never-show-expired-offers (requirement 3), outbound
send failure never crashing the inbound ack, and -- the key correctness
test for requirement 6 -- that a webhook redelivery after the marker was
never committed does NOT create a duplicate offer.
"""
import hashlib
import hmac
import json

import pytest

from backend.config import settings
from backend.messaging import WhatsAppVipRouter, reset_whatsapp_vip_router_for_tests, set_whatsapp_vip_router_for_tests
from backend.messaging.whatsapp_adapter import MetaApiError, MetaClient, SandboxMetaClient
from backend.messaging.whatsapp_vip_identity import authorize_whatsapp_id, create_vip_invite
from backend.models import ProcessedWebhookEventDB, WhatsAppVipOfferDB
from backend.security.rate_limit import reset_rate_limiter_for_tests


@pytest.fixture()
def vip_sandbox(db_session):
    client = SandboxMetaClient()
    set_whatsapp_vip_router_for_tests(WhatsAppVipRouter(client))
    reset_rate_limiter_for_tests()
    yield client
    reset_whatsapp_vip_router_for_tests()


class _FailingMetaClient(MetaClient):
    def send_message(self, to, text):
        raise MetaApiError("simulated failure")


@pytest.fixture()
def vip_failing(db_session):
    set_whatsapp_vip_router_for_tests(WhatsAppVipRouter(_FailingMetaClient()))
    reset_rate_limiter_for_tests()
    yield
    reset_whatsapp_vip_router_for_tests()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _envelope(message_id: str, whatsapp_id: str, text: str) -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "TEST_WABA_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "TEST_PHONE_NUMBER_ID"},
                                "contacts": [{"wa_id": whatsapp_id, "profile": {"name": "Synthetic"}}],
                                "messages": [{"id": message_id, "from": whatsapp_id, "timestamp": "1700000000", "type": "text", "text": {"body": text}}],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def _send(client, whatsapp_id: str, text: str, msg_id: str):
    body = _envelope(msg_id, whatsapp_id, text)
    return client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})


def _last_text_to(sandbox: SandboxMetaClient, whatsapp_id: str) -> str:
    for to, text in reversed(sandbox.sent):
        if to == whatsapp_id:
            return text
    return ""


def _create_full_offer(client, sandbox, db_session, whatsapp_id: str, *, msg_prefix: str, give="2", receive="1", give_method="1", receive_method="3") -> None:
    """Drives the whole wizard for one identity. Menu indices: currency menu
    order is USD,CUP,EUR,MLC,MXN (give=2 -> CUP); after excluding CUP the
    receive menu is USD,EUR,MLC,MXN (receive=1 -> USD)."""
    authorize_whatsapp_id(db_session, whatsapp_id)
    steps = [
        ("OFERTA", "start"),  # -> intent menu
        ("1", "intent"),      # answer COMPRO
        (give, "give_currency"),
        (receive, "receive_currency"),
        ("4000", "amount"),
        ("1", "rate_unit"),   # rate in give currency (CUP)
        ("445", "rate_value"),
        (give_method, "give_method"),
        ("La Habana", "give_location"),
        (receive_method, "receive_method"),
        ("Miami", "receive_location"),
        ("1", "duration"),    # 24h
        ("CONFIRMAR", "confirm"),
    ]
    for i, (text, _label) in enumerate(steps):
        r = _send(client, whatsapp_id, text, f"{msg_prefix}.{i}")
        assert r.status_code == 200, (i, text, r.text)


def test_two_identities_opposite_directions_full_wizard(client, vip_sandbox, db_session):
    sandbox = vip_sandbox
    buyer = "wa-buyer-1"
    seller = "wa-seller-1"

    _create_full_offer(client, sandbox, db_session, buyer, msg_prefix="wamid.buyer")
    offers = db_session.query(WhatsAppVipOfferDB).all()
    assert len(offers) == 1
    assert offers[0].give_currency.value == "CUP"
    assert offers[0].receive_currency.value == "USD"

    confirm_text = _last_text_to(sandbox, buyer)
    assert "publicada" in confirm_text.lower()
    assert "ACTIVA" in confirm_text

    # Second identity, VENDO direction (give USD, receive CUP). give=1 -> USD;
    # after excluding USD the receive menu is CUP,EUR,MLC,MXN, so receive=1 -> CUP.
    _create_full_offer(client, sandbox, db_session, seller, msg_prefix="wamid.seller", give="1", receive="1", give_method="3", receive_method="1")
    offers = db_session.query(WhatsAppVipOfferDB).order_by(WhatsAppVipOfferDB.id).all()
    assert len(offers) == 2
    assert offers[1].give_currency.value == "USD"
    assert offers[1].receive_currency.value == "CUP"


def test_offer_listing_never_shows_third_party_whatsapp_id(client, vip_sandbox, db_session):
    sandbox = vip_sandbox
    owner = "wa-privacy-owner"
    viewer = "wa-privacy-viewer"
    authorize_whatsapp_id(db_session, viewer)
    _create_full_offer(client, sandbox, db_session, owner, msg_prefix="wamid.privacy")

    r = _send(client, viewer, "OFERTAS", "wamid.privacy-browse")
    assert r.status_code == 200
    listing = _last_text_to(sandbox, viewer)
    assert owner not in listing


def test_interest_is_idempotent_and_notifies_owner_without_leaking_identity(client, vip_sandbox, db_session):
    sandbox = vip_sandbox
    owner = "wa-interest-owner-e2e"
    interested = "wa-interest-person-e2e"
    authorize_whatsapp_id(db_session, interested)
    _create_full_offer(client, sandbox, db_session, owner, msg_prefix="wamid.int")

    offer = db_session.query(WhatsAppVipOfferDB).first()

    r1 = _send(client, interested, f"ME INTERESA {offer.id}", "wamid.interest1")
    assert r1.status_code == 200
    owner_notification = _last_text_to(sandbox, owner)
    assert "interes" in owner_notification.lower()
    assert interested not in owner_notification

    sent_count_before = len(sandbox.sent)
    r2 = _send(client, interested, f"ME INTERESA {offer.id}", "wamid.interest2")
    assert r2.status_code == 200
    # second ME INTERESA is idempotent -- no second owner notification
    owner_notifications_after = [t for w, t in sandbox.sent if w == owner and "interes" in t.lower()]
    assert len(owner_notifications_after) == 1
    assert len(sandbox.sent) == sent_count_before + 1  # only the "ya habias mostrado interes" reply to the interested party


def test_close_offer_only_owner_and_idempotent(client, vip_sandbox, db_session):
    sandbox = vip_sandbox
    owner = "wa-close-owner-e2e"
    stranger = "wa-close-stranger-e2e"
    authorize_whatsapp_id(db_session, stranger)
    _create_full_offer(client, sandbox, db_session, owner, msg_prefix="wamid.closee2e")
    offer = db_session.query(WhatsAppVipOfferDB).first()

    r_stranger = _send(client, stranger, f"CERRAR {offer.id}", "wamid.close-stranger")
    assert r_stranger.status_code == 200
    assert "no se pudo cerrar" in _last_text_to(sandbox, stranger).lower()

    r1 = _send(client, owner, f"CERRAR {offer.id}", "wamid.close1")
    assert r1.status_code == 200
    assert "cerrada" in _last_text_to(sandbox, owner).lower()

    r2 = _send(client, owner, f"CERRAR {offer.id}", "wamid.close2")
    assert r2.status_code == 200
    assert "ya estaba cerrada" in _last_text_to(sandbox, owner).lower()


def test_unauthorized_identity_is_denied_for_every_vip_command(client, vip_sandbox, db_session):
    sandbox = vip_sandbox
    stranger = "wa-unauthorized-1"

    for i, cmd in enumerate(["OFERTA", "OFERTAS", "MIS OFERTAS", "ME INTERESA 1", "CERRAR 1"]):
        r = _send(client, stranger, cmd, f"wamid.unauth{i}")
        assert r.status_code == 200
        assert "no estas autorizado" in _last_text_to(sandbox, stranger).lower()

    assert db_session.query(WhatsAppVipOfferDB).count() == 0


def test_vip_invite_redemption_grants_access(client, vip_sandbox, db_session):
    sandbox = vip_sandbox
    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    newcomer = "wa-invite-e2e-1"

    r_before = _send(client, newcomer, "OFERTA", "wamid.before-invite")
    assert "no estas autorizado" in _last_text_to(sandbox, newcomer).lower()

    r_redeem = _send(client, newcomer, f"VIP {code}", "wamid.redeem")
    assert r_redeem.status_code == 200
    assert "autorizado" in _last_text_to(sandbox, newcomer).lower()

    r_after = _send(client, newcomer, "OFERTA", "wamid.after-invite")
    assert r_after.status_code == 200
    assert "que queres hacer" in _last_text_to(sandbox, newcomer).lower()


def test_expired_offer_never_shown_even_without_a_sweep(client, vip_sandbox, db_session):
    from datetime import timedelta

    from backend import timeutils

    sandbox = vip_sandbox
    owner = "wa-expiry-owner-e2e"
    viewer = "wa-expiry-viewer-e2e"
    authorize_whatsapp_id(db_session, viewer)
    _create_full_offer(client, sandbox, db_session, owner, msg_prefix="wamid.expiry")

    offer = db_session.query(WhatsAppVipOfferDB).first()
    offer.expires_at = timeutils.utcnow() - timedelta(hours=1)
    db_session.commit()

    r = _send(client, viewer, "OFERTAS", "wamid.expiry-browse")
    assert r.status_code == 200
    assert "no hay ofertas disponibles" in _last_text_to(sandbox, viewer).lower()


def test_outbound_send_failure_never_crashes_the_inbound_ack(client, vip_failing, db_session):
    authorize_whatsapp_id(db_session, "wa-sendfail-1")
    r = _send(client, "wa-sendfail-1", "OFERTA", "wamid.sendfail1")
    assert r.status_code == 200  # inbound webhook ack must never depend on outbound success

    from backend.models import OutboundMessageDB, OutboundMessageStatus

    row = db_session.query(OutboundMessageDB).filter(OutboundMessageDB.whatsapp_id == "wa-sendfail-1").first()
    assert row is not None
    assert row.status == OutboundMessageStatus.FAILED_RETRYABLE


def test_webhook_redelivery_after_marker_never_committed_does_not_duplicate_offer(client, vip_sandbox, db_session):
    """THE key test for requirement 6: simulates a crash between
    handle_inbound completing (offer created, reply enqueued) and the
    ProcessedWebhookEventDB marker actually landing -- by deleting that
    marker row right after the first successful request, then redelivering
    the EXACT same webhook. The offer must NOT be duplicated (protected by
    WhatsAppVipOfferDB.source_message_id's unique constraint via
    create_offer's idempotency check), even though the handler genuinely
    runs a second time end-to-end through the real HTTP route."""
    owner = "wa-crash-owner"
    authorize_whatsapp_id(db_session, owner)

    setup_steps = [
        ("OFERTA", "wamid.crash.0"), ("1", "wamid.crash.1"), ("2", "wamid.crash.2"), ("1", "wamid.crash.3"),
        ("4000", "wamid.crash.4"), ("1", "wamid.crash.5"), ("445", "wamid.crash.6"),
        ("1", "wamid.crash.7"), ("La Habana", "wamid.crash.8"), ("3", "wamid.crash.9"),
        ("Miami", "wamid.crash.10"), ("1", "wamid.crash.11"),
    ]
    for text, msg_id in setup_steps:
        r = _send(client, owner, text, msg_id)
        assert r.status_code == 200

    confirm_msg_id = "wamid.crash.confirm"
    r_confirm = _send(client, owner, "CONFIRMAR", confirm_msg_id)
    assert r_confirm.status_code == 200
    assert db_session.query(WhatsAppVipOfferDB).count() == 1

    # Simulate "the process crashed after handle_inbound committed the offer
    # and the outbox row, but before/during the marker commit" by deleting
    # the marker Meta's redelivery would otherwise be blocked by.
    marker = db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.provider == "whatsapp", ProcessedWebhookEventDB.message_id == confirm_msg_id).first()
    assert marker is not None  # it WAS committed on the happy path -- we're now removing it to simulate the crash window
    db_session.delete(marker)
    db_session.commit()

    # Redelivery: Meta resends the exact same CONFIRMAR message.
    r_redelivered = _send(client, owner, "CONFIRMAR", confirm_msg_id)
    assert r_redelivered.status_code == 200

    offers = db_session.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.source_message_id == confirm_msg_id).all()
    assert len(offers) == 1, "redelivery after a simulated crash must never create a duplicate offer"


def test_process_restart_recovery_sends_pending_outbound_message(db_session):
    """Simulates a restart with a pending reply that was never sent (crash
    happened right after enqueue, before attempt_send_one ran) -- the
    startup recovery sweep (flush_pending_outbound, called directly here
    the same way main.py's startup hook does) must pick it up and deliver
    it, without needing any new inbound webhook."""
    from backend.services import whatsapp_outbox

    row = whatsapp_outbox.enqueue(db_session, "wa-restart-1", "mensaje pendiente de antes del reinicio")

    client = SandboxMetaClient()
    result = whatsapp_outbox.flush_pending_outbound(db_session, client, limit=10)
    assert result["attempted"] == 1
    db_session.refresh(row)
    from backend.models import OutboundMessageStatus

    assert row.status == OutboundMessageStatus.META_ACCEPTED
    assert client.sent == [("wa-restart-1", "mensaje pendiente de antes del reinicio")]


def test_crash_between_wizard_advance_and_outbox_enqueue_leaves_nothing_partial(client, vip_sandbox, db_session, monkeypatch):
    """Audit fix regression test: reproduces the exact bug the atomicity fix
    closes. Before the fix, _set_state committed the wizard's session-state
    advance IMMEDIATELY, separately from (and before) the outbox reply's own
    commit -- a crash in that window left session.state already advanced
    with NO reply ever queued and NO marker recorded. Simulates that crash
    by making the outbox staging call raise right after _set_state has
    already mutated the in-memory session object, and asserts NOTHING
    (state, outbox row, marker) is left committed from that attempt."""
    from backend.models import ChannelIdentityDB, OutboundMessageDB, VipWizardState, WhatsAppVipSessionDB
    from backend.services import whatsapp_outbox as outbox_module

    owner = "wa-crash-advance-1"
    authorize_whatsapp_id(db_session, owner)

    r0 = _send(client, owner, "OFERTA", "wamid.crashadv.0")
    assert r0.status_code == 200

    session_row = (
        db_session.query(WhatsAppVipSessionDB)
        .join(ChannelIdentityDB, WhatsAppVipSessionDB.channel_identity_id == ChannelIdentityDB.id)
        .filter(ChannelIdentityDB.channel_user_id == owner)
        .first()
    )
    assert session_row.state == VipWizardState.WIZ_INTENT
    outbox_count_before = db_session.query(OutboundMessageDB).count()

    original_stage = outbox_module.stage_pending

    def _boom_once(*args, **kwargs):
        raise RuntimeError("simulated crash between wizard advance and outbox enqueue")

    monkeypatch.setattr(outbox_module, "stage_pending", _boom_once)
    with pytest.raises(RuntimeError):
        _send(client, owner, "1", "wamid.crashadv.1")  # answers WIZ_INTENT (COMPRO)
    monkeypatch.setattr(outbox_module, "stage_pending", original_stage)

    db_session.refresh(session_row)
    assert session_row.state == VipWizardState.WIZ_INTENT, "the crashed attempt must not leave the wizard advanced"
    assert db_session.query(OutboundMessageDB).count() == outbox_count_before, "no outbox row from the crashed attempt"
    assert (
        db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id == "wamid.crashadv.1").first() is None
    ), "no idempotency marker from the crashed attempt"

    # Redelivery: Meta resends the EXACT same message (it never got a 200 for the crashed attempt).
    r1 = _send(client, owner, "1", "wamid.crashadv.1")
    assert r1.status_code == 200
    db_session.refresh(session_row)
    assert session_row.state == VipWizardState.WIZ_GIVE_CURRENCY, "redelivery must correctly advance the wizard exactly once"


def test_resend_same_message_id_after_crash_applies_answer_to_correct_wizard_step(client, vip_sandbox, db_session, monkeypatch):
    """Distinguishes from the rollback-only assertion above by checking
    CONTENT correctness: proves a redelivered numeric answer at an
    intermediate wizard step is consumed by the QUESTION IT ACTUALLY
    ANSWERED, never silently shifted onto the next one -- which is exactly
    what the old (pre-fix) separately-committing _set_state allowed, since
    intermediate steps have no unique key of their own (unlike the final
    OFERTA confirmation's source_message_id) to protect them. Drives the
    wizard all the way to a real offer and checks its fields match what was
    actually intended by the redelivered answer."""
    from backend.services import whatsapp_outbox as outbox_module

    owner = "wa-resend-numeric-1"
    authorize_whatsapp_id(db_session, owner)
    r0 = _send(client, owner, "OFERTA", "wamid.resend.0")
    assert r0.status_code == 200

    original_stage = outbox_module.stage_pending

    def _boom_once(*args, **kwargs):
        raise RuntimeError("simulated crash mid wizard-step")

    monkeypatch.setattr(outbox_module, "stage_pending", _boom_once)
    with pytest.raises(RuntimeError):
        _send(client, owner, "1", "wamid.resend.1")  # intended answer: COMPRO
    monkeypatch.setattr(outbox_module, "stage_pending", original_stage)

    # Meta redelivers the EXACT same message (same id, same text) since it never got a 200.
    r1 = _send(client, owner, "1", "wamid.resend.1")
    assert r1.status_code == 200

    remaining = [
        ("2", "wamid.resend.2"), ("1", "wamid.resend.3"), ("4000", "wamid.resend.4"),
        ("1", "wamid.resend.5"), ("445", "wamid.resend.6"), ("1", "wamid.resend.7"),
        ("La Habana", "wamid.resend.8"), ("3", "wamid.resend.9"), ("Miami", "wamid.resend.10"),
        ("1", "wamid.resend.11"), ("CONFIRMAR", "wamid.resend.12"),
    ]
    for text, msg_id in remaining:
        r = _send(client, owner, text, msg_id)
        assert r.status_code == 200, (text, msg_id, r.text)

    offer = db_session.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.source_message_id == "wamid.resend.12").first()
    assert offer is not None, "the wizard must have completed and published an offer"
    assert offer.intent.value == "compro", "the redelivered '1' must answer the INTENT question it was sent for, not a later one"
    assert offer.give_currency.value == "CUP"
    assert offer.receive_currency.value == "USD"


def test_two_concurrent_deliveries_of_the_same_message_id_never_both_apply(db_session):
    """Two threads deliver the EXACT same inbound message (same message_id)
    at the same time, each through its OWN DB connection to the SAME
    underlying sqlite file (never sharing one SQLAlchemy Session across
    threads) -- like two real concurrent webhook deliveries hitting two
    request handlers. Calls the atomic unit directly (bypassing HTTP) so
    both threads share the SAME WhatsAppVipRouter instance, the way
    get_whatsapp_vip_router()'s singleton would in production -- this is
    what actually exercises the threading.local() fix for handle_inbound's
    per-call pending-sends list. Exactly one delivery must win; the other
    must leave nothing of its own durable."""
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.messaging.whatsapp_adapter import SandboxMetaClient
    from backend.messaging.whatsapp_vip_router import WhatsAppVipRouter
    from backend.models import ChannelIdentityDB, VipWizardState, WhatsAppVipSessionDB
    from backend.routers.whatsapp import _handle_vip_message_atomically
    from backend.services import whatsapp_outbox

    owner = "wa-concurrent-msg-1"
    authorize_whatsapp_id(db_session, owner)
    router = WhatsAppVipRouter(SandboxMetaClient())

    # Establish the session row via a real, uncontested first step, so the
    # race below is purely over the SHARED numeric-step message -- identity/
    # session row creation racing on a brand-new whatsapp_id is a separate,
    # pre-existing, out-of-scope gap (see whatsapp_vip_router.py's module
    # docstring).
    won0, pending0 = _handle_vip_message_atomically(db_session, router, owner, "OFERTA", "wamid.conc.0")
    assert won0 is True
    for row in pending0:
        whatsapp_outbox.attempt_send_one(db_session, row, router.meta_client)

    db_path = str(db_session.get_bind().url).replace("sqlite:///", "")
    engine_b = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionB = sessionmaker(autocommit=False, autoflush=False, bind=engine_b)
    db_b = SessionB()

    results: dict = {}

    def _deliver(label, db):
        results[label] = _handle_vip_message_atomically(db, router, owner, "1", "wamid.conc.1")

    ta = threading.Thread(target=_deliver, args=("a", db_session))
    tb = threading.Thread(target=_deliver, args=("b", db_b))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    db_b.close()

    wins = [label for label, (won, _rows) in results.items() if won]
    assert len(wins) == 1, f"exactly one concurrent delivery must win, got {results}"

    marker_count = db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id == "wamid.conc.1").count()
    assert marker_count == 1

    session_row = (
        db_session.query(WhatsAppVipSessionDB)
        .join(ChannelIdentityDB, WhatsAppVipSessionDB.channel_identity_id == ChannelIdentityDB.id)
        .filter(ChannelIdentityDB.channel_user_id == owner)
        .first()
    )
    db_session.refresh(session_row)
    assert session_row.state == VipWizardState.WIZ_GIVE_CURRENCY, "the wizard must advance exactly once, never twice or zero times"


def test_two_concurrent_deliveries_of_the_first_message_for_a_brand_new_whatsapp_id(db_session, monkeypatch):
    """Same shape as the concurrent-delivery test above, but for a
    whatsapp_id NEITHER thread has ever seen before -- exercises the gap
    whatsapp_vip_router.py's module docstring flagged as pre-existing and
    out of scope for the previous fix: get_or_create_identity()'s own
    internal commit (creating the FIRST ChannelIdentityDB row for this
    whatsapp_id) racing the same commit from a concurrent delivery. If this
    reproduces an uncaught IntegrityError propagating out of
    _handle_vip_message_atomically, that is a bug to fix, not a passing
    assertion -- both threads must always come back with either a clean win
    or a clean loss, never an unhandled exception, exactly one identity row
    must ever exist for this whatsapp_id, and exactly one marker for this
    message_id.

    Verified empirically (via a standalone diagnostic script, before writing
    this test) that CPython/SQLite rarely interleave two threads on their
    own inside stage_identity's narrow, I/O-light
    SELECT-then-INSERT-then-flush sequence -- an unforced version of this
    test could pass on pure luck without ever exercising the collision. So
    this test forces it deterministically: monkeypatches stage_identity
    (the function _dispatch actually calls -- NOT the committing
    get_or_create_identity wrapper, which is not on this call path) to
    sleep between its SELECT and its INSERT, widening the window so BOTH
    threads are guaranteed to pass the 'not found' check before either
    writes -- while still calling straight through to the real, unmodified
    function for everything else.

    Correction 2026-09-27: an earlier version of this test patched
    get_or_create_identity, which _dispatch stopped calling once it was
    changed to call stage_identity directly (same turn that introduced
    stage_identity) -- that monkeypatch had been silently inert ever
    since, and this test was passing without ever actually forcing the
    collision it claimed to."""
    import threading
    import time

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.messaging import whatsapp_vip_identity as identity_svc
    from backend.messaging.whatsapp_adapter import SandboxMetaClient
    from backend.messaging.whatsapp_vip_router import WhatsAppVipRouter
    from backend.models import Channel, ChannelIdentityDB
    from backend.routers.whatsapp import _handle_vip_message_atomically

    owner = "wa-brand-new-concurrent-1"  # never authorized, never seen by either thread before this
    router = WhatsAppVipRouter(SandboxMetaClient())

    original_stage_identity = identity_svc.stage_identity

    def _slow_stage_identity(db, whatsapp_id):
        identity = db.query(ChannelIdentityDB).filter(ChannelIdentityDB.channel == Channel.WHATSAPP, ChannelIdentityDB.channel_user_id == whatsapp_id).first()
        if identity is not None:
            return identity
        time.sleep(0.05)  # force BOTH threads past the SELECT above before either reaches the INSERT+flush
        return original_stage_identity(db, whatsapp_id)

    monkeypatch.setattr(identity_svc, "stage_identity", _slow_stage_identity)

    db_path = str(db_session.get_bind().url).replace("sqlite:///", "")
    engine_b = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionB = sessionmaker(autocommit=False, autoflush=False, bind=engine_b)
    db_b = SessionB()

    results: dict = {}
    errors: dict = {}
    barrier = threading.Barrier(2)

    def _deliver(label, db):
        barrier.wait()
        try:
            results[label] = _handle_vip_message_atomically(db, router, owner, "OFERTA", "wamid.brandnew.0")
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, this IS the thing under test
            errors[label] = exc

    ta = threading.Thread(target=_deliver, args=("a", db_session))
    tb = threading.Thread(target=_deliver, args=("b", db_b))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    db_b.close()

    assert errors == {}, f"neither concurrent delivery of a brand-new whatsapp_id's first message may raise uncaught -- got {errors}"

    wins = [label for label, (won, _rows) in results.items() if won]
    assert len(wins) == 1, f"exactly one concurrent delivery must win, got {results}"

    identity_count = db_session.query(ChannelIdentityDB).filter(ChannelIdentityDB.channel_user_id == owner).count()
    assert identity_count == 1, "exactly one identity row for this whatsapp_id, never two"

    marker_count = db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id == "wamid.brandnew.0").count()
    assert marker_count == 1


def test_crash_after_atomic_commit_before_immediate_send_sweep_delivers_exactly_once(db_session):
    """Simulates: the atomic unit (wizard/session-state advance + outbox
    PENDING row + webhook idempotency marker) already committed
    successfully via _handle_vip_message_atomically, but the process died
    right there -- BEFORE the caller's loop in receive_webhook got to call
    attempt_send_one for that row (the 'immediate best-effort send' step).
    The row is durably PENDING, so the startup/periodic sweep
    (flush_pending_outbound) must pick it up on its own -- exactly once,
    never zero times (it must not be lost) and never twice even if a stale
    immediate-send attempt for the SAME row also fires later (that is
    finding #2's atomic per-row claim in attempt_send_one, now verified
    against THIS specific ordering, not just two sweeps)."""
    from backend.messaging.whatsapp_adapter import SandboxMetaClient
    from backend.messaging.whatsapp_vip_router import WhatsAppVipRouter
    from backend.models import OutboundMessageStatus
    from backend.routers.whatsapp import _handle_vip_message_atomically
    from backend.services import whatsapp_outbox

    owner = "wa-crash-after-commit-1"
    authorize_whatsapp_id(db_session, owner)
    router = WhatsAppVipRouter(SandboxMetaClient())

    won, pending_sends = _handle_vip_message_atomically(db_session, router, owner, "OFERTA", "wamid.aftercommit.0")
    assert won is True
    assert len(pending_sends) == 1
    row = pending_sends[0]
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.PENDING  # durably queued, never attempted yet -- the "crash" happens right here

    # Startup/periodic sweep recovery (main.py's own pattern) -- no new inbound webhook involved.
    result = whatsapp_outbox.flush_pending_outbound(db_session, router.meta_client, limit=10)
    assert result["attempted"] == 1
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.META_ACCEPTED
    assert len(router.meta_client.sent) == 1, "the sweep must deliver the recovered row exactly once"
    sent_after_sweep = list(router.meta_client.sent)

    # A stale immediate-send attempt for the SAME row somehow still fires
    # after recovery (e.g. a delayed thread from before the crash) -- must
    # be a pure no-op, never a second real send.
    whatsapp_outbox.attempt_send_one(db_session, row, router.meta_client)
    assert router.meta_client.sent == sent_after_sweep, "a later attempt on an already-claimed row must never call Meta again"


def test_injected_failure_after_create_offer_before_response_rolls_back_and_redelivery_succeeds(client, vip_sandbox, db_session, monkeypatch):
    """Audit fix regression test (full commit-graph audit): create_offer no
    longer commits on its own -- injects a failure right AFTER it returns
    (offer flushed but NOT yet committed) and BEFORE the wizard's state
    reset / outbox reply are prepared. The whole atomic unit must roll back
    -- including the flushed-but-uncommitted offer row -- and a redelivery
    of the exact same CONFIRMAR message_id must then create the offer
    exactly once (not zero, not two)."""
    from backend.messaging import whatsapp_vip_router as router_module

    owner = "wa-fail-after-offer-1"
    authorize_whatsapp_id(db_session, owner)

    setup_steps = [
        ("OFERTA", "wamid.failoffer.0"), ("1", "wamid.failoffer.1"), ("2", "wamid.failoffer.2"),
        ("1", "wamid.failoffer.3"), ("4000", "wamid.failoffer.4"), ("1", "wamid.failoffer.5"),
        ("445", "wamid.failoffer.6"), ("1", "wamid.failoffer.7"), ("La Habana", "wamid.failoffer.8"),
        ("3", "wamid.failoffer.9"), ("Miami", "wamid.failoffer.10"), ("1", "wamid.failoffer.11"),
    ]
    for text, msg_id in setup_steps:
        r = _send(client, owner, text, msg_id)
        assert r.status_code == 200

    original_create_offer = router_module.create_offer

    def _create_offer_then_boom(*args, **kwargs):
        original_create_offer(*args, **kwargs)  # really creates it (flushed, uncommitted) before the injected crash
        raise RuntimeError("simulated crash right after create_offer, before preparing the response")

    monkeypatch.setattr(router_module, "create_offer", _create_offer_then_boom)

    confirm_msg_id = "wamid.failoffer.confirm"
    with pytest.raises(RuntimeError):
        _send(client, owner, "CONFIRMAR", confirm_msg_id)

    assert db_session.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.source_message_id == confirm_msg_id).count() == 0, "the injected failure must roll back the flushed-but-uncommitted offer too"
    assert db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id == confirm_msg_id).first() is None

    monkeypatch.setattr(router_module, "create_offer", original_create_offer)

    # Redelivery: Meta resends the exact same CONFIRMAR (it never got a 200).
    r = _send(client, owner, "CONFIRMAR", confirm_msg_id)
    assert r.status_code == 200
    offers = db_session.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.source_message_id == confirm_msg_id).all()
    assert len(offers) == 1, "redelivery after the injected failure must create the offer exactly once"
    assert "publicada" in _last_text_to(vip_sandbox, owner).lower()


def test_injected_failure_after_register_interest_before_response_rolls_back_and_redelivery_succeeds(client, vip_sandbox, db_session, monkeypatch):
    """Same shape, for register_interest: injects a failure right after it
    returns (interest row flushed, uncommitted) and before the confirmation
    reply to the interested party AND the notification to the owner are
    prepared. Both must be absent after the rollback, and redelivery of the
    same ME INTERESA message_id must register interest exactly once."""
    from backend.messaging import whatsapp_vip_router as router_module
    from backend.models import WhatsAppVipOfferInterestDB

    owner = "wa-fail-interest-owner-1"
    interested = "wa-fail-interest-person-1"
    authorize_whatsapp_id(db_session, interested)
    _create_full_offer(client, vip_sandbox, db_session, owner, msg_prefix="wamid.failint")
    offer = db_session.query(WhatsAppVipOfferDB).first()

    original_register_interest = router_module.register_interest

    def _register_interest_then_boom(*args, **kwargs):
        original_register_interest(*args, **kwargs)
        raise RuntimeError("simulated crash right after register_interest, before preparing the response")

    monkeypatch.setattr(router_module, "register_interest", _register_interest_then_boom)

    interest_msg_id = "wamid.failint.interest"
    with pytest.raises(RuntimeError):
        _send(client, interested, f"ME INTERESA {offer.id}", interest_msg_id)

    assert db_session.query(WhatsAppVipOfferInterestDB).count() == 0, "the injected failure must roll back the flushed-but-uncommitted interest row too"
    assert db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id == interest_msg_id).first() is None
    assert not any(t == interest_msg_id for _to, t in vip_sandbox.sent)  # sanity: nothing was ever actually sent for this attempt either

    monkeypatch.setattr(router_module, "register_interest", original_register_interest)

    r = _send(client, interested, f"ME INTERESA {offer.id}", interest_msg_id)
    assert r.status_code == 200
    assert db_session.query(WhatsAppVipOfferInterestDB).count() == 1, "redelivery after the injected failure must register interest exactly once"
    assert "interes" in _last_text_to(vip_sandbox, interested).lower()


def test_injected_failure_after_redeem_vip_invite_before_response_rolls_back_and_redelivery_succeeds(client, vip_sandbox, db_session, monkeypatch):
    """Same shape, for redeem_vip_invite: injects a failure right after it
    returns (membership flushed, uncommitted, invite.used_count already
    incremented in memory) and before the 'ya estas autorizado' reply is
    prepared. The rollback must undo the membership AND the used_count
    increment (otherwise a max_uses=1 code would be silently burned by a
    failed attempt); redelivery of the same VIP <code> message_id must then
    grant access exactly once."""
    from backend.messaging import whatsapp_vip_identity as identity_svc
    from backend.models import WhatsAppVipInviteDB, WhatsAppVipMembershipDB

    newcomer = "wa-fail-invite-1"
    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)

    original_redeem = identity_svc.redeem_vip_invite

    def _redeem_then_boom(*args, **kwargs):
        original_redeem(*args, **kwargs)
        raise RuntimeError("simulated crash right after redeem_vip_invite, before preparing the response")

    monkeypatch.setattr(identity_svc, "redeem_vip_invite", _redeem_then_boom)

    redeem_msg_id = "wamid.failredeem.0"
    with pytest.raises(RuntimeError):
        _send(client, newcomer, f"VIP {code}", redeem_msg_id)

    assert db_session.query(WhatsAppVipMembershipDB).count() == 0
    assert db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id == redeem_msg_id).first() is None
    invite = db_session.query(WhatsAppVipInviteDB).first()
    assert invite.used_count == 0, "the rollback must undo the used_count increment too -- a failed attempt must never burn a single-use code"

    monkeypatch.setattr(identity_svc, "redeem_vip_invite", original_redeem)

    r = _send(client, newcomer, f"VIP {code}", redeem_msg_id)
    assert r.status_code == 200
    assert db_session.query(WhatsAppVipMembershipDB).count() == 1, "redelivery after the injected failure must grant access exactly once"
    assert "autorizado" in _last_text_to(vip_sandbox, newcomer).lower()


def test_two_different_concurrent_messages_from_the_same_user_during_the_wizard(db_session):
    """Different from the same-message_id concurrency test: here BOTH
    messages have their OWN unique message_id (so the marker's uniqueness
    cannot arbitrate between them) and DIFFERENT text -- simulating a user
    double-tapping, e.g. answering the SAME wizard question with two
    different values in quick succession. Neither is a 'duplicate' by the
    marker's definition, so both commits may legitimately succeed; what
    this proves is that the session ends up in a SINGLE, well-defined state
    -- never a corrupted mix, never two session rows, never an uncaught
    exception -- even though nothing here enforces which of the two
    concurrent answers 'wins' (last-commit-wins is an accepted, documented
    limitation for two genuinely-concurrent, unordered inputs from the same
    user; there is no version/optimistic-lock column on the session row)."""
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.messaging.whatsapp_adapter import SandboxMetaClient
    from backend.messaging.whatsapp_vip_router import WhatsAppVipRouter
    from backend.models import ChannelIdentityDB, VipWizardState, WhatsAppVipSessionDB
    from backend.routers.whatsapp import _handle_vip_message_atomically

    owner = "wa-two-different-concurrent-1"
    authorize_whatsapp_id(db_session, owner)
    router = WhatsAppVipRouter(SandboxMetaClient())

    won0, _pending0 = _handle_vip_message_atomically(db_session, router, owner, "OFERTA", "wamid.twodiff.0")
    assert won0 is True  # real, uncontested first step -- reaches WIZ_INTENT

    db_path = str(db_session.get_bind().url).replace("sqlite:///", "")
    engine_b = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionB = sessionmaker(autocommit=False, autoflush=False, bind=engine_b)
    db_b = SessionB()

    results: dict = {}
    errors: dict = {}
    barrier = threading.Barrier(2)

    def _deliver(label, db, text, msg_id):
        barrier.wait()
        try:
            results[label] = _handle_vip_message_atomically(db, router, owner, text, msg_id)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, this IS the thing under test
            errors[label] = exc

    ta = threading.Thread(target=_deliver, args=("a", db_session, "1", "wamid.twodiff.a"))  # answers intent as COMPRO
    tb = threading.Thread(target=_deliver, args=("b", db_b, "2", "wamid.twodiff.b"))  # answers intent as VENDO
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    db_b.close()

    assert errors == {}, f"neither concurrent delivery may raise uncaught -- got {errors}"

    session_rows = (
        db_session.query(WhatsAppVipSessionDB)
        .join(ChannelIdentityDB, WhatsAppVipSessionDB.channel_identity_id == ChannelIdentityDB.id)
        .filter(ChannelIdentityDB.channel_user_id == owner)
        .all()
    )
    assert len(session_rows) == 1, "never two session rows for the same identity"

    db_session.refresh(session_rows[0])
    # THE ACTUAL, VERIFIED behavior (confirmed via a standalone diagnostic
    # before writing this assertion): SQLite's writer lock fully serializes
    # the two commits -- there is no lost update, no torn write, no
    # corruption. But there is also no ordering guarantee and no
    # optimistic-lock/version column on the session row, so whichever
    # commits SECOND reads the state the FIRST one already committed, not
    # the state that was current when the user actually sent it. Both
    # messages here answer WIZ_INTENT ("1"=COMPRO, "2"=VENDO); depending on
    # commit order the end state is either:
    #   - WIZ_GIVE_CURRENCY, if the second commit somehow also read
    #     WIZ_INTENT (both raced on the exact same initial read), or
    #   - WIZ_RECEIVE_CURRENCY, if the second commit read the first's
    #     already-advanced WIZ_GIVE_CURRENCY and its own text ("1" or "2")
    #     got legitimately consumed as a give_currency selection instead.
    # Either is a valid, singular, non-corrupted state -- exactly what a
    # user firing two real messages before waiting for the bot's first
    # reply would also produce with NO concurrency involved at all. This is
    # a documented characteristic of a stateful wizard with no message-level
    # sequencing, not a data-corruption bug -- so this test asserts
    # "well-defined and non-corrupted", not "matches one specific answer".
    assert session_rows[0].state in (VipWizardState.WIZ_GIVE_CURRENCY, VipWizardState.WIZ_RECEIVE_CURRENCY), (
        f"unexpected/corrupted state: {session_rows[0].state}, context={session_rows[0].context}"
    )

    markers = db_session.query(ProcessedWebhookEventDB).filter(ProcessedWebhookEventDB.message_id.in_(["wamid.twodiff.a", "wamid.twodiff.b"])).count()
    wins = sum(1 for won, _rows in results.values() if won)
    assert wins == 2, "both messages have distinct message_ids, so neither is a 'duplicate' -- both commits legitimately succeed"
    assert markers == wins, "exactly one marker per successful commit, never more"


def test_two_identities_concurrently_redeeming_the_same_max_uses_one_invite_grants_exactly_one_membership(db_session):
    """Audit fix regression test: redeem_vip_invite's used_count reservation
    is now an atomic UPDATE (code not revoked, not expired, used_count <
    max_uses, all in the SAME statement -- see whatsapp_vip_identity.py's
    revision note), not a Python read-then-increment. TWO DIFFERENT
    identities, each with its OWN session/message_id, redeem the SAME
    max_uses=1 invite code through TWO INDEPENDENT DB connections at the
    same time. Both message-level commits legitimately succeed (distinct
    message_ids, so the webhook marker never arbitrates between them) --
    what must hold is that exactly ONE of them actually created a
    membership, and the invite's used_count ends at exactly 1, never 2.

    Both identities AND their session rows are pre-staged (via one real,
    synchronous, throwaway message each, through the SAME
    _handle_vip_message_atomically the race itself uses) rather than left
    brand-new. Verified via a standalone diagnostic before writing this
    test: SQLite only allows one writer at a time, and _dispatch creates a
    WhatsAppVipSessionDB row unconditionally for ANY first-ever message
    (before it even looks at whether the text starts with "VIP ") -- so if
    either identity or session still needed its own first-ever INSERT
    during the race, that write alone would hold the file's write lock for
    that thread's ENTIRE transaction (including this test's whole
    redemption path), forcing the other thread to block until the first
    fully committed -- serializing the two attempts and masking the actual
    used_count race long before either reached it. With both rows already
    existing, everything up to the final reservation is a plain read, so
    both threads genuinely reach the check at the same time -- reproduced
    the intended double-grant reliably against the old read-then-increment
    code during that diagnostic, and never against the atomic fix below."""
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.messaging.whatsapp_adapter import SandboxMetaClient
    from backend.messaging.whatsapp_vip_router import WhatsAppVipRouter
    from backend.models import WhatsAppVipInviteDB, WhatsAppVipMembershipDB
    from backend.routers.whatsapp import _handle_vip_message_atomically

    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    router = WhatsAppVipRouter(SandboxMetaClient())
    won_a, _ = _handle_vip_message_atomically(db_session, router, "wa-invite-race-a", "hola", "wamid.inviterace.prestage-a")
    won_b, _ = _handle_vip_message_atomically(db_session, router, "wa-invite-race-b", "hola", "wamid.inviterace.prestage-b")
    assert won_a is True and won_b is True  # both identity+session rows now exist and are committed

    db_path = str(db_session.get_bind().url).replace("sqlite:///", "")
    engine_b = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionB = sessionmaker(autocommit=False, autoflush=False, bind=engine_b)
    db_b = SessionB()

    results: dict = {}
    errors: dict = {}
    barrier = threading.Barrier(2)

    def _deliver(label, db, whatsapp_id, msg_id):
        barrier.wait()
        try:
            results[label] = _handle_vip_message_atomically(db, router, whatsapp_id, f"VIP {code}", msg_id)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, this IS the thing under test
            errors[label] = exc

    ta = threading.Thread(target=_deliver, args=("a", db_session, "wa-invite-race-a", "wamid.inviterace.a"))
    tb = threading.Thread(target=_deliver, args=("b", db_b, "wa-invite-race-b", "wamid.inviterace.b"))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    db_b.close()

    assert errors == {}, f"neither concurrent redemption may raise uncaught -- got {errors}"

    wins = [label for label, (won, _rows) in results.items() if won]
    assert len(wins) == 2, f"both message_ids are distinct -- both webhook-level commits legitimately succeed, got {results}"

    memberships = db_session.query(WhatsAppVipMembershipDB).count()
    assert memberships == 1, "exactly one membership must be granted for a max_uses=1 code, no matter how the two redemptions race"

    invite = db_session.query(WhatsAppVipInviteDB).first()
    assert invite.used_count == 1, f"used_count must be exactly 1 after the race, got {invite.used_count}"


def test_wallet_flow_is_completely_unaffected_by_vip_routing(client, vip_sandbox, db_session, fake_chain):
    """Sanity check that VIP dispatch never intercepts a wallet-flow command
    -- MENU is not a VIP keyword, must still reach the old ConversationRouter."""
    from backend.messaging import ConversationRouter, WhatsAppAdapter, reset_conversation_router_for_tests, set_conversation_router_for_tests

    wallet_client = SandboxMetaClient()
    set_conversation_router_for_tests(ConversationRouter(WhatsAppAdapter(wallet_client)))
    try:
        r = _send(client, "wa-wallet-untouched-1", "MENU", "wamid.wallet1")
        assert r.status_code == 200
        # the wallet flow's own SandboxMetaClient (a SEPARATE instance from
        # vip_sandbox) should have received the reply, not the VIP one
        assert any(to == "wa-wallet-untouched-1" for to, _ in wallet_client.sent)
        assert not any(to == "wa-wallet-untouched-1" for to, _ in vip_sandbox.sent)
    finally:
        reset_conversation_router_for_tests()
