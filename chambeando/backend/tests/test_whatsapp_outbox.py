"""
WhatsApp VIP outbox -- covers the state machine described in
services/whatsapp_outbox.py's module docstring: enqueue-before-send,
retry-with-backoff, terminal failure after exhausting attempts, the
stuck-SENDING -> UNCERTAIN_NEEDS_RECONCILIATION path (never auto-retried),
manual reconciliation, status-webhook correlation, and the DB lease.
"""
from datetime import timedelta

import pytest

from backend import timeutils
from backend.messaging.whatsapp_adapter import MetaApiError, MetaClient
from backend.models import OutboundMessageDB, OutboundMessageStatus, OutboxWorkerLeaseDB
from backend.services import whatsapp_outbox as svc


class _AlwaysSucceedsClient(MetaClient):
    def __init__(self):
        self.sent = []

    def send_message(self, to, text):
        self.sent.append((to, text))

    def send_message_with_id(self, to, text):
        self.send_message(to, text)
        return f"wamid.ok-{len(self.sent)}"


class _AlwaysFailsClient(MetaClient):
    def send_message(self, to, text):
        raise MetaApiError("simulated permanent failure")


class _FlakyThenSucceedsClient(MetaClient):
    """Fails the first N calls, then succeeds -- simulates a transient Meta
    outage that recovers, exercising the retry path."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def send_message(self, to, text):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise MetaApiError("simulated transient failure")

    def send_message_with_id(self, to, text):
        self.send_message(to, text)
        return "wamid.recovered"


def test_enqueue_persists_before_any_send_attempt(db_session):
    row = svc.enqueue(db_session, "wa-1", "hola")
    assert row.status == OutboundMessageStatus.PENDING
    assert row.id is not None
    # still there after a fresh query -- durable, not just in-memory
    reloaded = db_session.query(OutboundMessageDB).filter(OutboundMessageDB.id == row.id).first()
    assert reloaded.status == OutboundMessageStatus.PENDING


def test_attempt_send_one_success_records_wamid(db_session):
    row = svc.enqueue(db_session, "wa-2", "hola")
    client = _AlwaysSucceedsClient()
    svc.attempt_send_one(db_session, row, client)
    assert row.status == OutboundMessageStatus.META_ACCEPTED
    assert row.wamid == "wamid.ok-1"
    assert row.attempt_count == 1
    assert client.sent == [("wa-2", "hola")]


def test_attempt_send_one_never_marks_delivered_on_success(db_session):
    """META_ACCEPTED != DELIVERED -- delivered only comes from a real status webhook."""
    row = svc.enqueue(db_session, "wa-3", "hola")
    svc.attempt_send_one(db_session, row, _AlwaysSucceedsClient())
    assert row.status != OutboundMessageStatus.DELIVERED


def test_attempt_send_one_retries_on_failure_before_exhausting_attempts(db_session):
    row = svc.enqueue(db_session, "wa-4", "hola")
    client = _AlwaysFailsClient()
    svc.attempt_send_one(db_session, row, client)
    assert row.status == OutboundMessageStatus.FAILED_RETRYABLE
    assert row.attempt_count == 1
    assert row.next_attempt_at is not None


def test_attempt_send_one_reaches_terminal_after_max_attempts(db_session):
    from backend.config import settings

    row = svc.enqueue(db_session, "wa-5", "hola")
    client = _AlwaysFailsClient()
    for _ in range(settings.WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS):
        svc.attempt_send_one(db_session, row, client)
    assert row.status == OutboundMessageStatus.FAILED_TERMINAL
    assert row.attempt_count == settings.WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS


def test_flush_pending_outbound_recovers_after_transient_failure(db_session):
    """Simulates the periodic sweep picking up a PENDING row, failing once,
    then succeeding on a later sweep once Meta recovers -- exactly the
    'periodic retries while the process runs' requirement."""
    row = svc.enqueue(db_session, "wa-6", "hola")
    client = _FlakyThenSucceedsClient(fail_times=1)

    result1 = svc.flush_pending_outbound(db_session, client, limit=10)
    assert result1["lease_acquired"] is True
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.FAILED_RETRYABLE

    # force next_attempt_at into the past so the second sweep considers it due
    row.next_attempt_at = timeutils.utcnow() - timedelta(seconds=1)
    db_session.commit()

    result2 = svc.flush_pending_outbound(db_session, client, limit=10)
    assert result2["attempted"] == 1
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.META_ACCEPTED
    assert row.wamid == "wamid.recovered"


def test_flush_pending_outbound_never_retries_before_next_attempt_at(db_session):
    row = svc.enqueue(db_session, "wa-7", "hola")
    svc.attempt_send_one(db_session, row, _AlwaysFailsClient())
    assert row.status == OutboundMessageStatus.FAILED_RETRYABLE
    assert timeutils.ensure_utc(row.next_attempt_at) > timeutils.utcnow()

    result = svc.flush_pending_outbound(db_session, _AlwaysSucceedsClient(), limit=10)
    assert result["attempted"] == 0  # not due yet -- backoff respected


def test_reconcile_stuck_sending_moves_to_uncertain_never_retries(db_session):
    """Simulates a process crash between 'marked SENDING' and 'recorded the
    outcome' -- reconcile must move it to UNCERTAIN, never resend."""
    row = svc.enqueue(db_session, "wa-8", "hola")
    row.status = OutboundMessageStatus.SENDING
    row.updated_at = timeutils.utcnow() - timedelta(seconds=999)
    db_session.commit()

    from backend.config import settings

    count = svc.reconcile_stuck_sending(db_session, settings.WHATSAPP_VIP_OUTBOX_STUCK_SECONDS)
    assert count == 1
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION

    # flush must NEVER pick up an UNCERTAIN row and resend it
    client = _AlwaysSucceedsClient()
    svc.flush_pending_outbound(db_session, client, limit=10)
    assert client.sent == []


def test_recently_sending_row_is_not_touched_by_reconcile(db_session):
    row = svc.enqueue(db_session, "wa-9", "hola")
    row.status = OutboundMessageStatus.SENDING
    row.updated_at = timeutils.utcnow()  # just started, not stuck
    db_session.commit()

    from backend.config import settings

    count = svc.reconcile_stuck_sending(db_session, settings.WHATSAPP_VIP_OUTBOX_STUCK_SECONDS)
    assert count == 0
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.SENDING


def test_manual_reconciliation_confirmed_sent(db_session):
    row = svc.enqueue(db_session, "wa-10", "hola")
    row.status = OutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION
    db_session.commit()

    updated = svc.mark_confirmed_sent(db_session, row, wamid="wamid.manually-confirmed")
    assert updated.status == OutboundMessageStatus.META_ACCEPTED
    assert updated.wamid == "wamid.manually-confirmed"


def test_manual_reconciliation_confirmed_not_sent_requeues(db_session):
    row = svc.enqueue(db_session, "wa-11", "hola")
    row.status = OutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION
    db_session.commit()

    updated = svc.mark_confirmed_not_sent(db_session, row)
    assert updated.status == OutboundMessageStatus.PENDING

    client = _AlwaysSucceedsClient()
    result = svc.flush_pending_outbound(db_session, client, limit=10)
    assert result["attempted"] == 1
    assert client.sent == [("wa-11", "hola")]


def test_list_uncertain_returns_only_uncertain_rows(db_session):
    row1 = svc.enqueue(db_session, "wa-12", "a")
    row2 = svc.enqueue(db_session, "wa-13", "b")
    row1.status = OutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION
    db_session.commit()

    uncertain = svc.list_uncertain(db_session)
    assert [r.id for r in uncertain] == [row1.id]
    assert row2.id not in [r.id for r in uncertain]


def test_apply_status_update_delivered(db_session):
    row = svc.enqueue(db_session, "wa-14", "hola")
    svc.attempt_send_one(db_session, row, _AlwaysSucceedsClient())
    wamid = row.wamid

    applied = svc.apply_status_update(db_session, wamid, "delivered")
    assert applied is True
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.DELIVERED


def test_apply_status_update_failed(db_session):
    row = svc.enqueue(db_session, "wa-15", "hola")
    svc.attempt_send_one(db_session, row, _AlwaysSucceedsClient())
    wamid = row.wamid

    applied = svc.apply_status_update(db_session, wamid, "failed")
    assert applied is True
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.FAILED_TERMINAL


def test_apply_status_update_unknown_wamid_is_a_safe_noop(db_session):
    applied = svc.apply_status_update(db_session, "wamid.never-seen", "delivered")
    assert applied is False


def test_apply_status_update_never_overrides_uncertain(db_session):
    """A status webhook arriving for a row already marked UNCERTAIN must not
    silently resolve it automatically -- only mark_confirmed_* (operator
    action) does."""
    row = svc.enqueue(db_session, "wa-16", "hola")
    row.wamid = "wamid.manual-case"
    row.status = OutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION
    db_session.commit()

    applied = svc.apply_status_update(db_session, "wamid.manual-case", "delivered")
    assert applied is False
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION


def test_lease_prevents_two_concurrent_sweeps_from_both_running(db_session):
    """Simulates two workers racing: only the one that wins the CAS actually
    processes rows this tick."""
    svc.enqueue(db_session, "wa-17", "hola")

    acquired_a = svc.acquire_lease(db_session, "worker-a", lease_seconds=20)
    acquired_b = svc.acquire_lease(db_session, "worker-b", lease_seconds=20)
    assert acquired_a is True
    assert acquired_b is False  # worker-a still holds it


def test_lease_can_be_reacquired_after_expiry(db_session):
    svc.acquire_lease(db_session, "worker-a", lease_seconds=1)
    lease = db_session.query(OutboxWorkerLeaseDB).first()
    lease.lease_expires_at = timeutils.utcnow() - timedelta(seconds=1)
    db_session.commit()

    assert svc.acquire_lease(db_session, "worker-b", lease_seconds=20) is True


def test_two_concurrent_attempts_never_both_call_meta_for_the_same_row(db_session):
    """Audit fix regression test for finding #2. Reproduces main.py's real
    overlap risk directly: _run_outbox_sweep_once always uses the SAME
    _PROCESS_WORKER_TOKEN (by design -- flush_pending_outbound's docstring:
    a tick must renew its OWN still-valid lease instead of losing the CAS to
    itself), so the coarse per-sweep lease alone does NOT stop two
    overlapping sweeps in the SAME process (e.g. the startup sweep still
    running via asyncio.to_thread when the first periodic tick also fires)
    from both selecting the SAME due row. This test drives attempt_send_one's
    own atomic per-row claim directly with two real threads and two
    independent DB connections to the same file -- bypassing the
    lease/sweep machinery entirely, because the claim must hold regardless
    of how two attempts ended up holding the same row."""
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    row = svc.enqueue(db_session, "wa-two-sweeps-1", "hola")

    db_path = str(db_session.get_bind().url).replace("sqlite:///", "")
    engine_b = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionB = sessionmaker(autocommit=False, autoflush=False, bind=engine_b)
    db_b = SessionB()

    client = _AlwaysSucceedsClient()
    barrier = threading.Barrier(2)

    def _attempt(db):
        row_local = db.query(OutboundMessageDB).filter(OutboundMessageDB.id == row.id).first()
        barrier.wait()
        svc.attempt_send_one(db, row_local, client)

    ta = threading.Thread(target=_attempt, args=(db_session,))
    tb = threading.Thread(target=_attempt, args=(db_b,))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    db_b.close()

    assert len(client.sent) == 1, f"Meta must be called exactly once for this row, got {client.sent}"
    db_session.refresh(row)
    assert row.status == OutboundMessageStatus.META_ACCEPTED
    assert row.attempt_count == 1


def test_flush_pending_outbound_noop_when_lease_not_acquired(db_session):
    svc.enqueue(db_session, "wa-18", "hola")
    svc.acquire_lease(db_session, "someone-else", lease_seconds=60)

    client = _AlwaysSucceedsClient()
    result = svc.flush_pending_outbound(db_session, client, limit=10)
    assert result == {"lease_acquired": False}
    assert client.sent == []
