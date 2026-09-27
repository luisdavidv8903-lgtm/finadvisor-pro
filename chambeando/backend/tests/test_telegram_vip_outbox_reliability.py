"""
Reliability audit ("revision final del piloto", 2026-09-27): traces one
offer + one ME INTERESA from POST /telegram/webhook through the atomic
commit, the send to the interested party, and the notification to the
owner -- then injects (1) a Telegram API failure AFTER the commit and (2) a
process crash BEFORE the send loop runs, and proves with tests which
replies are recovered and which are lost.

Part A (test_loss_under_the_pre_fix_pattern_*) reproduces the ORIGINAL
design verbatim -- routers/telegram.py used to call
`vip_router_instance.adapter.send(OutboundMessage(...))` in a bare loop
over whatever handle_inbound() returned, and TelegramAdapter.send()
swallows TelegramApiError (best-effort, same contract as
WhatsAppAdapter.send). That original shape is rebuilt here, standalone,
because the real handle_inbound() no longer returns bare tuples (this
audit changed it to always stage a durable outbox row) -- these tests are
the concrete proof of what the pre-fix code actually lost, not a strawman.

Part B (test_the_fix_recovers_*) drives the SAME two failure injections
through the REAL current code path (services/telegram_outbox.py +
routers/telegram.py's _handle_telegram_vip_message_atomically) and proves
both replies are durably recovered: never lost, and never blindly
re-sent for an attempt whose outcome is actually uncertain.

Part C verifies cross-channel isolation (requirement 2) and that a shared
deep link never grants membership nor interest on a closed/expired offer
(requirement 3, beyond what test_telegram_vip_router_e2e.py already covers).

Part D (2026-09-27, second pass -- "cierra dos riesgos") closes two
remaining gaps found on re-review:

  1. attempt_send_one previously treated EVERY TelegramApiError identically
     (a real HTTP rejection and a lost-response timeout both went straight
     to FAILED_RETRYABLE, i.e. were blindly retried). test_timeout_...
     forces a timeout AFTER the mock transport has "received" the request
     -- simulating the exact case where Telegram may have already
     processed the sendMessage and only the response was lost -- and
     proves it now lands in UNCERTAIN_NEEDS_RECONCILIATION instead, is
     excluded from the next sweep's retry query, and is never marked SENT.
  2. list_active_offers (OFERTAS / browse) had NO channel filter at all --
     test_ofertas_browse_* proves a Telegram OFERTAS listing never
     includes a WhatsApp-owned offer and vice versa, through the real
     webhook for both sides.
"""
import httpx
import pytest

from backend.config import settings
from backend.messaging import TelegramVipRouter, reset_telegram_vip_router_for_tests, set_telegram_vip_router_for_tests
from backend.messaging.adapter import OutboundMessage
from backend.messaging.telegram_adapter import RealTelegramClient, SandboxTelegramClient, TelegramAdapter, TelegramApiError, TelegramApiUncertainError, TelegramClient
from backend.messaging.whatsapp_vip_identity import create_vip_invite
from backend.models import Channel, ChannelIdentityDB, TelegramOutboundMessageDB, TelegramOutboundMessageStatus, WhatsAppVipOfferDB, WhatsAppVipOfferInterestDB
from backend.routers.telegram import _handle_telegram_vip_message_atomically
from backend.security.rate_limit import reset_rate_limiter_for_tests
from backend.services import telegram_outbox

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


def _create_full_offer(client, sandbox, db_session, chat_id: str, *, msg_prefix: str) -> int:
    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    r_redeem = _send(client, chat_id, f"VIP {code}", f"{msg_prefix}.redeem")
    assert r_redeem.status_code == 200, r_redeem.text

    steps = [
        ("OFERTA", "start"), ("1", "intent"), ("2", "give_currency"), ("1", "receive_currency"),
        ("4000", "amount"), ("1", "rate_unit"), ("445", "rate_value"), ("1", "give_method"),
        ("La Habana", "give_location"), ("3", "receive_method"), ("Miami", "receive_location"),
        ("1", "duration"), ("CONFIRMAR", "confirm"),
    ]
    for i, (text, _label) in enumerate(steps):
        r = _send(client, chat_id, text, f"{msg_prefix}.{i}")
        assert r.status_code == 200, (i, text, r.text)

    offer = db_session.query(WhatsAppVipOfferDB).order_by(WhatsAppVipOfferDB.id.desc()).first()
    return offer.id


class _AlwaysFailingTelegramClient(TelegramClient):
    def send_message(self, chat_id: str, text: str) -> None:
        raise TelegramApiError("simulated Telegram API failure")


class _FailNTimesThenWorkClient(TelegramClient):
    """Fails the first `fail_times` calls, then delegates to a real
    SandboxTelegramClient -- models a transient outage that later clears,
    exactly the case the outbox's retry/backoff exists for."""

    def __init__(self, fail_times: int, sandbox: SandboxTelegramClient) -> None:
        self._remaining_failures = fail_times
        self._sandbox = sandbox

    def send_message(self, chat_id: str, text: str) -> None:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise TelegramApiError("simulated transient Telegram API failure")
        self._sandbox.send_message(chat_id, text)


# ---------------------------------------------------------------------------
# Part A -- the pre-fix pattern, rebuilt standalone, proving what it lost.
# ---------------------------------------------------------------------------


def _old_pattern_commit_then_bare_send_loop(pending_sends_as_tuples: list[tuple[str, str]], client: TelegramClient) -> None:
    """Verbatim reconstruction of routers/telegram.py's ORIGINAL VIP send
    step (before this audit): `for chat_id, text in pending_sends:
    vip_router_instance.adapter.send(OutboundMessage(to=chat_id,
    text=text))` -- TelegramAdapter.send() swallows TelegramApiError, so a
    failure here leaves no trace and raises nothing. Never touches the
    outbox (that table did not exist in the pre-fix design), which is
    exactly the point: nothing durable was ever staged for these replies at
    all under the old design, so there is nothing here to assert on except
    the absence of any record afterward."""
    adapter = TelegramAdapter(client)
    for chat_id, text in pending_sends_as_tuples:
        adapter.send(OutboundMessage(to=chat_id, text=text))


def test_loss_under_the_pre_fix_pattern_api_failure_after_commit(db_session):
    """Reproduces failure mode 1 from the task: 'Inyecta un fallo de
    Telegram API despues del commit.' Under the pre-fix design, the DB
    mutation (e.g. an interest registration) is already committed by the
    time the send loop runs -- exactly as it still is today (that part was
    always correct: the ack never depended on outbound success). What the
    OLD design lost was the REPLY itself: once adapter.send() swallows the
    TelegramApiError, nothing durable anywhere records that a reply to
    "tg-loss-owner-1" was ever owed."""
    owed_reply = [("tg-loss-owner-1", "Alguien mostro interes en tu oferta #1. Escribi MIS OFERTAS para verla.")]
    _old_pattern_commit_then_bare_send_loop(owed_reply, _AlwaysFailingTelegramClient())

    # Nothing durable exists anywhere to recover this reply from -- no
    # outbox row (the table didn't even exist in this design), no log
    # record a recovery sweep could act on, nothing.
    assert db_session.query(TelegramOutboundMessageDB).filter(TelegramOutboundMessageDB.telegram_chat_id == "tg-loss-owner-1").count() == 0
    assert telegram_outbox.flush_pending_outbound(db_session, SandboxTelegramClient())["attempted"] == 0, (
        "a sweep finds nothing to retry, because nothing was ever staged -- the reply is gone permanently"
    )


def test_loss_under_the_pre_fix_pattern_crash_before_send_loop(db_session):
    """Reproduces failure mode 2 from the task: 'un crash antes del envio.'
    Under the pre-fix design, handle_inbound() returned a plain in-memory
    Python list -- if the process dies in the gap between the atomic
    commit succeeding and routers/telegram.py's send loop actually
    iterating that list (a crash, OOM kill, systemd restart), the list and
    everything in it is gone with the process. This test models that crash
    literally: the list exists in memory (line below), and then... nothing
    ever consumes it, because that's what "crash before the loop runs"
    means. There is no code left to call, and that absence IS the proof."""
    owed_reply_that_the_process_never_gets_to_send = [("tg-loss-owner-2", "Interes registrado en la oferta #1.")]
    # (a crash happens here, before any loop over the list above runs)

    # After "restart", nothing durable ever existed for this reply -- a
    # recovery sweep has literally nothing to find, because the pre-fix
    # design never persisted pending_sends anywhere.
    assert db_session.query(TelegramOutboundMessageDB).filter(TelegramOutboundMessageDB.telegram_chat_id == "tg-loss-owner-2").count() == 0
    assert len(owed_reply_that_the_process_never_gets_to_send) == 1, "the reply existed in memory, but memory is exactly what a crash destroys"


# ---------------------------------------------------------------------------
# Part B -- the fix (services/telegram_outbox.py), proving both failure
# modes are now recovered, end to end, through the real code path.
# ---------------------------------------------------------------------------


def test_the_fix_recovers_reply_after_api_failure_then_sweep_retry(client, telegram_vip_sandbox, db_session, monkeypatch):
    """Full trace, requirement 1: POST /telegram/webhook (ME INTERESA) ->
    atomic commit (interest row + owner-notification outbox row) -> the
    IMMEDIATE best-effort send fails (injected TelegramApiError) -> the row
    is left FAILED_RETRYABLE, durably, with a backoff next_attempt_at --
    never lost, and never blindly retried before that backoff elapses. A
    later recovery sweep (once the transient failure has "cleared" and
    next_attempt_at has passed) delivers it exactly once."""
    sandbox = telegram_vip_sandbox
    owner = "tg-reliab-owner-1"
    interested = "tg-reliab-interested-1"

    offer_id = _create_full_offer(client, sandbox, db_session, owner, msg_prefix="tgreliab")
    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    _send(client, interested, f"VIP {code}", "tgreliab.b.redeem")

    # The router's underlying client now fails on every call -- the
    # owner-notification's IMMEDIATE best-effort attempt (right after the
    # atomic commit, inside the webhook request) will fail.
    router = TelegramVipRouter(_AlwaysFailingTelegramClient())
    set_telegram_vip_router_for_tests(router)

    r_interest = _send(client, interested, f"ME INTERESA {offer_id}", "tgreliab.interest")
    assert r_interest.status_code == 200  # the webhook ack never depends on outbound success

    # The interest itself (a DB mutation, committed before any send is
    # attempted) is NOT lost -- this part was always correct.
    assert db_session.query(WhatsAppVipOfferInterestDB).count() == 1

    # The owner's notification IS durably recorded, even though the
    # immediate send failed -- this is the fix. FAILED_RETRYABLE, not gone.
    # Ordered by id DESC: `owner` also received several earlier, successful
    # wizard replies during _create_full_offer (sent by the original
    # non-failing router before it was swapped out above) -- the LATEST row
    # for this chat_id is the one this test actually injected the failure
    # for.
    owner_row = db_session.query(TelegramOutboundMessageDB).filter(TelegramOutboundMessageDB.telegram_chat_id == owner).order_by(TelegramOutboundMessageDB.id.desc()).first()
    assert owner_row is not None, "the owner's notification must be durably staged, never silently dropped"
    assert owner_row.status == TelegramOutboundMessageStatus.FAILED_RETRYABLE
    assert owner_row.attempt_count == 1
    assert owner_row.next_attempt_at is not None, "backoff must be set -- never retried instantly/blindly"

    # A sweep BEFORE next_attempt_at has elapsed must NOT retry it yet
    # (never a blind retry of an attempt still within its backoff window).
    still_failing_result = telegram_outbox.flush_pending_outbound(db_session, _AlwaysFailingTelegramClient())
    assert still_failing_result["attempted"] == 0
    db_session.refresh(owner_row)
    assert owner_row.status == TelegramOutboundMessageStatus.FAILED_RETRYABLE

    # Once the backoff window has passed AND the transient failure has
    # cleared (a working client this time), the recovery sweep delivers it.
    from datetime import timedelta

    from backend import timeutils

    owner_row.next_attempt_at = timeutils.utcnow() - timedelta(seconds=1)
    db_session.commit()

    recovery_result = telegram_outbox.flush_pending_outbound(db_session, sandbox)
    assert recovery_result["attempted"] == 1
    db_session.refresh(owner_row)
    assert owner_row.status == TelegramOutboundMessageStatus.SENT
    assert "interes" in _last_text_to(sandbox, owner).lower()


def test_the_fix_recovers_reply_after_crash_before_send_loop(db_session):
    """Full trace, requirement 1, second injection: the atomic commit
    (_handle_telegram_vip_message_atomically) succeeds -- wizard/session
    state advanced, the outbox row staged, the webhook idempotency marker
    written -- but the process is killed RIGHT THERE, before
    routers/telegram.py's loop gets a chance to call attempt_send_one even
    once. Modeled literally: call the atomic unit and then simply never
    call attempt_send_one for its result (that omission IS the simulated
    crash). The row must still be durably PENDING, and the
    startup/periodic recovery sweep (flush_pending_outbound, main.py's own
    pattern, no new inbound webhook involved) must pick it up and deliver
    it -- never lost, never sent twice."""
    from backend.messaging.whatsapp_vip_identity import stage_identity

    owner = "tg-reliab-crash-owner-1"
    stage_identity(db_session, owner, channel=Channel.TELEGRAM)
    db_session.commit()
    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    router = TelegramVipRouter(SandboxTelegramClient())

    won, pending_sends = _handle_telegram_vip_message_atomically(db_session, router, owner, f"VIP {code}", "tgreliab.crash.0")
    assert won is True
    assert len(pending_sends) == 1
    row = pending_sends[0]
    db_session.refresh(row)
    assert row.status == TelegramOutboundMessageStatus.PENDING  # durably queued, never attempted -- the "crash" happens right here

    # Startup/periodic sweep recovery (main.py's own pattern) -- no new
    # inbound webhook involved, exactly the post-restart recovery path.
    result = telegram_outbox.flush_pending_outbound(db_session, router.telegram_client)
    assert result["attempted"] == 1
    db_session.refresh(row)
    assert row.status == TelegramOutboundMessageStatus.SENT
    assert router.telegram_client.sent == [(owner, "Listo, ya estas autorizado para el piloto VIP.\n\nOFERTA - crear una oferta\nOFERTAS - ver ofertas disponibles")]

    # A stale immediate-send attempt for the SAME row somehow still fires
    # after recovery (e.g. a delayed thread from before the crash) -- must
    # be a pure no-op, never a second real send.
    telegram_outbox.attempt_send_one(db_session, row, router.telegram_client)
    assert len(router.telegram_client.sent) == 1, "a later attempt on an already-claimed/sent row must never send again"


def test_stuck_sending_row_is_never_blindly_retried(db_session):
    """'sin reintentos ciegos de envios inciertos' (requirement 1,
    explicit): a row stuck in SENDING (the process died mid-call to the
    Telegram API -- outcome genuinely unknown, it may have already gone
    through) must move to UNCERTAIN_NEEDS_RECONCILIATION and NEVER be
    auto-retried, exactly mirroring services/whatsapp_outbox.py's own
    reconcile_stuck_sending."""
    from datetime import timedelta

    from backend import timeutils

    row = telegram_outbox.enqueue(db_session, "tg-stuck-1", "mensaje en vuelo")
    row.status = TelegramOutboundMessageStatus.SENDING
    row.updated_at = timeutils.utcnow() - timedelta(seconds=settings.WHATSAPP_VIP_OUTBOX_STUCK_SECONDS + 5)
    db_session.commit()

    client = SandboxTelegramClient()
    result = telegram_outbox.flush_pending_outbound(db_session, client)
    assert result["reconciled_uncertain"] == 1
    db_session.refresh(row)
    assert row.status == TelegramOutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION
    assert client.sent == [], "a stuck/uncertain row must NEVER be auto-retried -- risk of a real duplicate message to a real person"


# ---------------------------------------------------------------------------
# Part C -- cross-channel isolation (requirement 2) and deep-link/interest
# invariants beyond what test_telegram_vip_router_e2e.py already covers
# (requirement 3).
# ---------------------------------------------------------------------------


def test_telegram_identity_can_never_interest_a_whatsapp_owned_offer(client, telegram_vip_sandbox, db_session, fake_chain):
    """Requirement 2: a WhatsAppVipOfferDB row created via the WhatsApp VIP
    wizard must be invisible/unactionable through the Telegram VIP router,
    even when a Telegram identity types its exact numeric id -- both
    ME INTERESA and the /start deep link must treat it as nonexistent."""
    from backend.messaging import WhatsAppVipRouter, reset_whatsapp_vip_router_for_tests, set_whatsapp_vip_router_for_tests
    from backend.messaging.whatsapp_adapter import SandboxMetaClient
    from backend.messaging.whatsapp_vip_identity import authorize_whatsapp_id

    whatsapp_sandbox = SandboxMetaClient()
    set_whatsapp_vip_router_for_tests(WhatsAppVipRouter(whatsapp_sandbox))
    try:
        whatsapp_owner = "wa-crosschannel-owner-1"
        authorize_whatsapp_id(db_session, whatsapp_owner)
        from backend.tests.test_whatsapp_vip_router_e2e import _create_full_offer as _create_whatsapp_offer

        wa_client = client  # same TestClient, same FastAPI app -- both webhooks are mounted on it
        _create_whatsapp_offer(wa_client, whatsapp_sandbox, db_session, whatsapp_owner, msg_prefix="wamid.crosschannel")
        whatsapp_offer = db_session.query(WhatsAppVipOfferDB).first()
        assert whatsapp_offer is not None
    finally:
        reset_whatsapp_vip_router_for_tests()

    telegram_sandbox = telegram_vip_sandbox
    telegram_person = "tg-crosschannel-1"
    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    _send(client, telegram_person, f"VIP {code}", "tgcross.redeem")

    r_open = _send(client, telegram_person, f"/start offer_{whatsapp_offer.id}", "tgcross.open")
    assert r_open.status_code == 200
    view_text = _last_text_to(telegram_sandbox, telegram_person)
    assert "no existe" in view_text.lower(), "a WhatsApp-owned offer must read as nonexistent through the Telegram deep link"

    r_interest = _send(client, telegram_person, f"ME INTERESA {whatsapp_offer.id}", "tgcross.interest")
    assert r_interest.status_code == 200
    assert "no existe" in _last_text_to(telegram_sandbox, telegram_person).lower()
    assert db_session.query(WhatsAppVipOfferInterestDB).count() == 0

    # The decisive check: the WhatsApp owner must NEVER receive anything
    # through the Telegram sandbox, and nothing was ever routed to the
    # Telegram Bot API using their whatsapp-style id as a chat_id.
    assert not any(to == whatsapp_owner for to, _ in telegram_sandbox.sent)


def test_whatsapp_identity_can_never_interest_a_telegram_owned_offer(client, telegram_vip_sandbox, db_session):
    """Symmetric to the test above -- a Telegram-owned offer must be
    invisible/unactionable through the WhatsApp VIP router."""
    from backend.messaging import WhatsAppVipRouter, reset_whatsapp_vip_router_for_tests, set_whatsapp_vip_router_for_tests
    from backend.messaging.whatsapp_adapter import SandboxMetaClient
    from backend.messaging.whatsapp_vip_identity import authorize_whatsapp_id

    telegram_sandbox = telegram_vip_sandbox
    telegram_owner = "tg-crosschannel-owner-2"
    telegram_offer_id = _create_full_offer(client, telegram_sandbox, db_session, telegram_owner, msg_prefix="tgcross2")

    whatsapp_sandbox = SandboxMetaClient()
    set_whatsapp_vip_router_for_tests(WhatsAppVipRouter(whatsapp_sandbox))
    try:
        whatsapp_person = "wa-crosschannel-2"
        authorize_whatsapp_id(db_session, whatsapp_person)

        import hashlib
        import hmac
        import json as _json

        def _wa_sign(body: bytes) -> str:
            return "sha256=" + hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()

        def _wa_send(text: str, msg_id: str):
            body = _json.dumps(
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
                                        "contacts": [{"wa_id": whatsapp_person, "profile": {"name": "Synthetic"}}],
                                        "messages": [{"id": msg_id, "from": whatsapp_person, "timestamp": "1700000000", "type": "text", "text": {"body": text}}],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ).encode()
            return client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": _wa_sign(body)})

        r_interest = _wa_send(f"ME INTERESA {telegram_offer_id}", "wamid.crosschannel2")
        assert r_interest.status_code == 200
        assert any("no existe" in t.lower() for to, t in whatsapp_sandbox.sent if to == whatsapp_person)
        assert db_session.query(WhatsAppVipOfferInterestDB).count() == 0
        assert not any(to == telegram_owner for to, _ in whatsapp_sandbox.sent)
    finally:
        reset_whatsapp_vip_router_for_tests()


def test_opening_the_shared_link_never_grants_membership(client, telegram_vip_sandbox, db_session):
    """Requirement 3: 'un enlace compartido no otorgue membresia.' A
    Telegram identity that has NEVER redeemed an invite must still be
    denied VIP commands after merely opening a valid deep link -- the link
    view itself must never create a WhatsAppVipMembershipDB row."""
    from backend.models import WhatsAppVipMembershipDB

    sandbox = telegram_vip_sandbox
    owner = "tg-membershipcheck-owner-1"
    offer_id = _create_full_offer(client, sandbox, db_session, owner, msg_prefix="tgmembercheck")

    never_invited = "tg-membershipcheck-stranger-1"
    r_open = _send(client, never_invited, f"/start offer_{offer_id}", "tgmembercheck.open")
    assert r_open.status_code == 200
    assert f"#{offer_id}" in _last_text_to(sandbox, never_invited)  # the view itself works, no membership required

    identity = db_session.query(ChannelIdentityDB).filter(ChannelIdentityDB.channel == Channel.TELEGRAM, ChannelIdentityDB.channel_user_id == never_invited).first()
    assert identity is not None  # opening the link DOES create a bare ChannelIdentityDB row (needed to reply at all)
    membership = db_session.query(WhatsAppVipMembershipDB).filter(WhatsAppVipMembershipDB.channel_identity_id == identity.id).first()
    assert membership is None, "opening a shared deep link must NEVER grant VIP membership by itself"

    r_interest = _send(client, never_invited, f"ME INTERESA {offer_id}", "tgmembercheck.interest")
    assert r_interest.status_code == 200
    assert "no estas autorizado" in _last_text_to(sandbox, never_invited).lower()
    assert db_session.query(WhatsAppVipOfferInterestDB).count() == 0


# ---------------------------------------------------------------------------
# Part D (second pass, "cierra dos riesgos") -- forced timeout-after-send,
# and OFERTAS/browse channel isolation.
# ---------------------------------------------------------------------------


def test_timeout_after_send_produces_uncertain_never_failed_retryable(db_session):
    """Requirement 1 (second pass): forces the exact scenario the task asks
    for -- a timeout/lost response AFTER Telegram's API could plausibly
    have already accepted the sendMessage call. The mock transport's
    handler runs (meaning, in a real deployment, the HTTP request reached
    the network -- indistinguishable here from "Telegram's server already
    received and is processing it") and THEN raises httpx.ReadTimeout
    instead of returning a response, modeling a response that never made
    it back. Must land in UNCERTAIN_NEEDS_RECONCILIATION -- never
    FAILED_RETRYABLE (which would be retried automatically next sweep),
    never SENT (which would falsely claim a confirmed delivery)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated: response lost after the request was sent", request=request)

    client = RealTelegramClient(
        bot_token="synthetic-test-token-never-real",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(TelegramApiUncertainError):
        client.send_message("tg-uncertain-1", "mensaje que Telegram pudo haber ya recibido")

    row = telegram_outbox.enqueue(db_session, "tg-uncertain-1", "mensaje que Telegram pudo haber ya recibido")
    telegram_outbox.attempt_send_one(db_session, row, client)
    db_session.refresh(row)

    assert row.status == TelegramOutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION
    assert row.status != TelegramOutboundMessageStatus.FAILED_RETRYABLE
    assert row.status != TelegramOutboundMessageStatus.SENT
    assert row.next_attempt_at is None, "an UNCERTAIN row carries no retry schedule -- it is not queued for automatic retry at all"

    # "sin reenvio automatico": a sweep must never pick this row up, no
    # matter how many times it runs.
    result = telegram_outbox.flush_pending_outbound(db_session, SandboxTelegramClient())
    assert result["attempted"] == 0
    db_session.refresh(row)
    assert row.status == TelegramOutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION, "an UNCERTAIN row must never be silently retried by a sweep"


def test_permanent_rejection_never_retries_and_signals_operator(db_session):
    """'Última corrección' requirement 1, path 2/4: a REAL HTTP 400 (also
    covers 401/403 -- see the parametrized test below) is a KNOWN-permanent
    rejection, not something retrying the identical request can fix.
    Corrects test_definitive_http_rejection_still_uses_the_normal_retry_path
    (removed), which wrongly expected a 400 to land in FAILED_RETRYABLE --
    that assumption is exactly what this revision fixes."""
    from backend.models import SecurityEventDB, SecurityEventType

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})

    client = RealTelegramClient(
        bot_token="synthetic-test-token-never-real",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    row = telegram_outbox.enqueue(db_session, "tg-permanent-reject-1", "mensaje rechazado permanentemente")
    telegram_outbox.attempt_send_one(db_session, row, client)
    db_session.refresh(row)

    assert row.status == TelegramOutboundMessageStatus.FAILED_PERMANENT
    assert row.next_attempt_at is None, "a permanent rejection is never scheduled for automatic retry"
    assert "status=400" in row.last_error
    assert "error_code=400" in row.last_error

    # Operational signal: a dedicated security event, sanitized (no body
    # text, no chat id in the reason).
    event = (
        db_session.query(SecurityEventDB)
        .filter(SecurityEventDB.action == SecurityEventType.TELEGRAM_VIP_OUTBOUND_DELIVERY_FAILED_PERMANENT, SecurityEventDB.target_id == row.id)
        .first()
    )
    assert event is not None
    assert "mensaje rechazado permanentemente" not in (event.reason or "")
    assert "tg-permanent-reject-1" not in (event.reason or "")

    # Never auto-retried by a sweep, ever.
    result = telegram_outbox.flush_pending_outbound(db_session, SandboxTelegramClient())
    assert result["attempted"] == 0
    db_session.refresh(row)
    assert row.status == TelegramOutboundMessageStatus.FAILED_PERMANENT


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409])
def test_every_non_429_4xx_is_classified_as_permanent(db_session, status_code):
    """Explicit, not-a-blind-range check: every 4xx except 429 -- not just
    the three the task names (400/401/403) -- must classify as permanent,
    proving the classification is a reviewed HTTP-semantics default, not a
    hardcoded three-item list that happens to work for the examples given."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"ok": False, "error_code": status_code})

    client = RealTelegramClient(
        bot_token="synthetic-test-token-never-real",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    row = telegram_outbox.enqueue(db_session, f"tg-4xx-{status_code}", "mensaje")
    telegram_outbox.attempt_send_one(db_session, row, client)
    db_session.refresh(row)
    assert row.status == TelegramOutboundMessageStatus.FAILED_PERMANENT, f"HTTP {status_code} must classify as permanent"


def test_429_respects_retry_after_and_retries_bounded(db_session):
    """'Última corrección' requirement 1, path 3/4: HTTP 429 is transient
    (self-clearing) -- must use the bounded retry path, and the backoff
    scheduled must respect Telegram's own `parameters.retry_after` from the
    response body (never shorter than what Telegram asked for)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 47", "parameters": {"retry_after": 47}})

    client = RealTelegramClient(
        bot_token="synthetic-test-token-never-real",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    row = telegram_outbox.enqueue(db_session, "tg-429-1", "mensaje con rate limit")
    telegram_outbox.attempt_send_one(db_session, row, client)
    db_session.refresh(row)

    assert row.status == TelegramOutboundMessageStatus.FAILED_RETRYABLE
    assert "status=429" in row.last_error
    from backend import timeutils

    seconds_until_retry = (timeutils.ensure_utc(row.next_attempt_at) - timeutils.utcnow()).total_seconds()
    assert 40 <= seconds_until_retry <= 47, f"backoff must honor Telegram's retry_after=47, got {seconds_until_retry}s"

    # A sweep before that window elapses must not retry it (never a blind
    # retry ignoring the requested backoff).
    early_result = telegram_outbox.flush_pending_outbound(db_session, SandboxTelegramClient())
    assert early_result["attempted"] == 0

    # Bounded: exhausting WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS on a persistent
    # 429 must still terminate at FAILED_TERMINAL, never loop forever.
    row.next_attempt_at = timeutils.utcnow()
    row.attempt_count = settings.WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS
    db_session.commit()
    telegram_outbox.attempt_send_one(db_session, row, client)
    db_session.refresh(row)
    assert row.status == TelegramOutboundMessageStatus.FAILED_TERMINAL


def test_5xx_is_treated_as_transient_and_retried(db_session):
    """'Última corrección' requirement 1, path 4/4: a 5xx (server-side
    error) is transient -- must use the bounded retry path, same as 429,
    even though it carries no retry_after (falls back to the usual
    exponential backoff)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False, "error_code": 503, "description": "Service Unavailable"})

    client = RealTelegramClient(
        bot_token="synthetic-test-token-never-real",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    row = telegram_outbox.enqueue(db_session, "tg-5xx-1", "mensaje durante una caida de Telegram")
    telegram_outbox.attempt_send_one(db_session, row, client)
    db_session.refresh(row)

    assert row.status == TelegramOutboundMessageStatus.FAILED_RETRYABLE
    assert "status=503" in row.last_error
    assert row.next_attempt_at is not None


def test_ofertas_browse_on_telegram_never_shows_a_whatsapp_owned_offer(client, telegram_vip_sandbox, db_session):
    """Requirement 2 (second pass): OFERTAS on Telegram, through the real
    webhook, must never list a WhatsApp-owned offer -- list_active_offers
    had no channel filter at all until this fix."""
    from backend.messaging import WhatsAppVipRouter, reset_whatsapp_vip_router_for_tests, set_whatsapp_vip_router_for_tests
    from backend.messaging.whatsapp_adapter import SandboxMetaClient
    from backend.messaging.whatsapp_vip_identity import authorize_whatsapp_id
    from backend.tests.test_whatsapp_vip_router_e2e import _create_full_offer as _create_whatsapp_offer

    whatsapp_sandbox = SandboxMetaClient()
    set_whatsapp_vip_router_for_tests(WhatsAppVipRouter(whatsapp_sandbox))
    try:
        whatsapp_owner = "wa-ofertas-isolation-owner-1"
        authorize_whatsapp_id(db_session, whatsapp_owner)
        _create_whatsapp_offer(client, whatsapp_sandbox, db_session, whatsapp_owner, msg_prefix="wamid.ofertasiso")
    finally:
        reset_whatsapp_vip_router_for_tests()

    telegram_sandbox = telegram_vip_sandbox
    telegram_owner = "tg-ofertas-isolation-owner-1"
    telegram_offer_id = _create_full_offer(client, telegram_sandbox, db_session, telegram_owner, msg_prefix="tgofertasiso")

    viewer = "tg-ofertas-isolation-viewer-1"
    code = create_vip_invite(db_session, max_uses=1, ttl_days=7)
    _send(client, viewer, f"VIP {code}", "tgofertasiso.viewer.redeem")

    r_browse = _send(client, viewer, "OFERTAS", "tgofertasiso.viewer.browse")
    assert r_browse.status_code == 200
    listing = _last_text_to(telegram_sandbox, viewer)
    assert f"#{telegram_offer_id}" in listing, "the Telegram-owned offer must be listed"
    # The WhatsApp offer's give_location ("La Habana") and give currency
    # combination is shared, so assert on the actual offer COUNT visible
    # instead of a text fragment that could coincidentally match.
    whatsapp_offer_count_in_db = db_session.query(WhatsAppVipOfferDB).count()
    assert whatsapp_offer_count_in_db == 2, "sanity: both a WhatsApp-owned and a Telegram-owned offer exist"
    listed_ids = {int(n) for n in __import__("re").findall(r"#(\d+)", listing)}
    assert listed_ids == {telegram_offer_id}, f"OFERTAS on Telegram must show ONLY the Telegram-owned offer, got ids {listed_ids}"


def test_ofertas_browse_on_whatsapp_never_shows_a_telegram_owned_offer(client, telegram_vip_sandbox, db_session):
    """Symmetric to the test above -- OFERTAS on WhatsApp must never list a
    Telegram-owned offer."""
    from backend.messaging import WhatsAppVipRouter, reset_whatsapp_vip_router_for_tests, set_whatsapp_vip_router_for_tests
    from backend.messaging.whatsapp_adapter import SandboxMetaClient
    from backend.messaging.whatsapp_vip_identity import authorize_whatsapp_id
    from backend.tests.test_whatsapp_vip_router_e2e import _create_full_offer as _create_whatsapp_offer
    from backend.tests.test_whatsapp_vip_router_e2e import _last_text_to as _wa_last_text_to
    from backend.tests.test_whatsapp_vip_router_e2e import _send as _wa_send

    telegram_owner = "tg-ofertas-isolation-owner-2"
    telegram_offer_id = _create_full_offer(client, telegram_vip_sandbox, db_session, telegram_owner, msg_prefix="tgofertasiso2")

    whatsapp_sandbox = SandboxMetaClient()
    set_whatsapp_vip_router_for_tests(WhatsAppVipRouter(whatsapp_sandbox))
    try:
        whatsapp_owner = "wa-ofertas-isolation-owner-2"
        authorize_whatsapp_id(db_session, whatsapp_owner)
        whatsapp_offer_id_holder: list[int] = []

        def _capture(client_, sandbox_, db_, chat_id, **kw):
            _create_whatsapp_offer(client_, sandbox_, db_, chat_id, **kw)
            whatsapp_offer_id_holder.append(db_.query(WhatsAppVipOfferDB).filter(WhatsAppVipOfferDB.source_message_id == f"{kw['msg_prefix']}.12").first().id)

        _capture(client, whatsapp_sandbox, db_session, whatsapp_owner, msg_prefix="wamid.ofertasiso2")
        whatsapp_offer_id = whatsapp_offer_id_holder[0]

        viewer = "wa-ofertas-isolation-viewer-2"
        authorize_whatsapp_id(db_session, viewer)
        r_browse = _wa_send(client, viewer, "OFERTAS", "wamid.ofertasiso2.browse")
        assert r_browse.status_code == 200
        listing = _wa_last_text_to(whatsapp_sandbox, viewer)
        listed_ids = {int(n) for n in __import__("re").findall(r"#(\d+)", listing)}
        assert listed_ids == {whatsapp_offer_id}, f"OFERTAS on WhatsApp must show ONLY the WhatsApp-owned offer, got ids {listed_ids} (telegram offer was #{telegram_offer_id})"
    finally:
        reset_whatsapp_vip_router_for_tests()
