"""
WhatsApp VIP outbox -- durable outbound delivery for the VIP flow.

Design goal (per the source evidence document's "Fiabilidad necesaria"
section): distinguish inbound-accepted / offer-saved / send-requested /
Meta-accepted / delivered as SEPARATE facts, persist the pending response
BEFORE acknowledging the inbound webhook, and recover safely across
crashes/restarts -- without ever promising exactly-once delivery (that is
not achievable against a real HTTP API + a process that can die mid-call).

State machine (OutboundMessageStatus):

    PENDING --------------------> SENDING --------------------> META_ACCEPTED --(status webhook)--> DELIVERED
       ^                             |  \\                             |
       |                             |   \\ (clean failure,            |
       | (operator confirms          |    \\ attempts left)            | (status webhook says failed)
       |  "never actually sent")     |     v                           v
       |                             |   FAILED_RETRYABLE ---------> FAILED_TERMINAL
       |                             |     (attempts exhausted)
       |                             |
       |                             v (stuck > STUCK_SECONDS -- process
       |                                likely died mid-call, outcome unknown)
       +-------------------- UNCERTAIN_NEEDS_RECONCILIATION

THE UNCERTAINTY WINDOW, explicitly: between "we called
MetaClient.send_message_with_id()" and "we recorded PENDING/FAILED/
META_ACCEPTED for that attempt", the process can be killed (crash, OOM,
`systemctl restart`, host reboot). In that exact window, Meta may have
already accepted and even delivered the message -- we simply never got to
record the wamid. A row stuck in SENDING past WHATSAPP_VIP_OUTBOX_STUCK_SECONDS
is treated as UNCERTAIN, NEVER auto-retried (a blind retry here risks
sending a real duplicate message to a real person -- worse than a missed
one). It is surfaced for manual/tooled reconciliation instead: an operator
can check Meta's own message log / ask the recipient, then call
mark_confirmed_sent() or mark_confirmed_not_sent() to resolve it explicitly.
No code path in this module ever auto-resolves an UNCERTAIN row.

Retries ARE periodic while the process runs (not just once at startup) --
see main.py's startup/shutdown hooks, which call flush_pending_outbound() on
a loop gated by settings.WHATSAPP_VIP_OUTBOX_AUTOSTART. A lightweight DB
lease (OutboxWorkerLeaseDB, acquired via atomic compare-and-swap UPDATE)
keeps exactly one worker responsible for a sweep at a time -- but that lease
is coarse (per-sweep, not per-row), and main.py's own two callers
(the startup recovery pass and the periodic loop) share the SAME
_PROCESS_WORKER_TOKEN on purpose (so a tick renews its own still-valid lease
instead of losing the CAS to itself, see flush_pending_outbound's
docstring) -- which means the lease alone does NOT stop two overlapping
sweeps IN THE SAME PROCESS (e.g. the startup sweep still running via
asyncio.to_thread when the first periodic tick also fires) from both
selecting the SAME due row. attempt_send_one's own atomic claim (below) is
what actually prevents Meta being called twice for one row, independent of
whatever let two attempts reach it.

Revision 2026-09-27 (audit finding): attempt_send_one's transition into
SENDING is now itself an atomic compare-and-swap (same UPDATE-with-WHERE
pattern as acquire_lease), re-checking the row's status is still
PENDING/FAILED_RETRYABLE in the SAME statement the database executes.
Whoever loses that race (two overlapping sweeps sharing this process's
token, or a periodic sweep racing the immediate best-effort send right
after enqueue) calls Meta zero times for that attempt -- never a blind
"read status, then decide" that two callers could both pass.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .. import timeutils
from ..config import settings
from ..messaging.whatsapp_adapter import MetaApiError, MetaClient
from ..models import OutboundMessageDB, OutboundMessageStatus, OutboxWorkerLeaseDB, SecurityEventType
from ..security.audit import log_security_event

logger = logging.getLogger("chambeando.whatsapp_vip.outbox")

_LEASE_ROW_ID = 1
_PROCESS_WORKER_TOKEN = uuid.uuid4().hex


def enqueue(db: Session, whatsapp_id: str, text: str, *, source_message_id: str | None = None) -> OutboundMessageDB:
    """Standalone durable enqueue -- commits immediately. For callers outside
    the VIP inbound webhook path (tests exercising the outbox in isolation,
    the process-restart recovery path, or any future direct caller). The VIP
    router's inbound handling does NOT use this directly -- see
    stage_pending below, which it uses instead so the reply lands in the
    SAME atomic commit as the wizard's session-state advance and the
    webhook idempotency marker (see routers/whatsapp.py's per-message
    atomic unit and whatsapp_vip_router.py's handle_inbound)."""
    row = stage_pending(db, whatsapp_id, text, source_message_id=source_message_id)
    db.commit()
    db.refresh(row)
    return row


def stage_pending(db: Session, whatsapp_id: str, text: str, *, source_message_id: str | None = None) -> OutboundMessageDB:
    """Adds a PENDING row to the session WITHOUT committing -- the caller is
    responsible for committing it as part of its own atomic unit (see
    enqueue() above for the old standalone-and-immediately-durable
    behavior). db.flush() (not commit) so row.id is available to the caller
    right away without ending the surrounding transaction."""
    row = OutboundMessageDB(whatsapp_id=whatsapp_id, body_text=text, source_message_id=source_message_id)
    db.add(row)
    db.flush()
    return row


def _ensure_lease_row(db: Session) -> None:
    if db.query(OutboxWorkerLeaseDB).filter(OutboxWorkerLeaseDB.id == _LEASE_ROW_ID).first() is not None:
        return
    db.add(OutboxWorkerLeaseDB(id=_LEASE_ROW_ID, owner_token=None, lease_expires_at=None))
    try:
        db.commit()
    except Exception:  # another worker raced us to create row 1 -- fine either way
        db.rollback()


def acquire_lease(db: Session, worker_token: str, lease_seconds: int) -> bool:
    """Atomic compare-and-swap: succeeds if no one holds the lease, the
    previous lease expired, or WE already hold it (renewal). Never a
    read-then-write -- the WHERE clause is checked by the database itself in
    the same UPDATE, so two processes racing this at once can never both
    believe they won."""
    _ensure_lease_row(db)
    now = timeutils.utcnow()
    # synchronize_session=False: this is a single-row lease record we never
    # read back off a stale in-memory object afterward (callers only care
    # about rowcount) -- ORM-level session sync would otherwise evaluate the
    # WHERE clause in Python against the loaded row, comparing a naive
    # datetime (SQLite always returns naive, see timeutils.py) against the
    # aware `now` above and raising TypeError. Plain SQL comparison (what
    # actually decides the CAS) has no such issue -- SQLite compares the
    # stored ISO8601 text correctly either way.
    result = db.execute(
        update(OutboxWorkerLeaseDB)
        .where(OutboxWorkerLeaseDB.id == _LEASE_ROW_ID)
        .where(
            or_(
                OutboxWorkerLeaseDB.lease_expires_at.is_(None),
                OutboxWorkerLeaseDB.lease_expires_at < now,
                OutboxWorkerLeaseDB.owner_token == worker_token,
            )
        )
        .values(owner_token=worker_token, lease_expires_at=now + timedelta(seconds=lease_seconds))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def _backoff_seconds(attempt_count: int) -> int:
    return min(300, 5 * (2**attempt_count))


def attempt_send_one(db: Session, row: OutboundMessageDB, adapter: MetaClient) -> None:
    """Marks SENDING durably BEFORE calling Meta (so a crash mid-call leaves
    unambiguous evidence an attempt was in flight -- see module docstring),
    then records exactly one of: META_ACCEPTED, FAILED_RETRYABLE, or
    FAILED_TERMINAL. Never resolves to UNCERTAIN itself -- that only happens
    later, from reconcile_stuck_sending, for a row whose SENDING attempt
    never got a chance to finish this function at all.

    The transition into SENDING is an ATOMIC compare-and-swap, same pattern
    as acquire_lease: the UPDATE's WHERE clause re-checks the row is still
    PENDING/FAILED_RETRYABLE in the SAME statement the database executes,
    never a Python-level "read status, then decide" that two callers could
    both pass. This is what actually stops two overlapping sweeps -- even
    two sharing this process's SAME worker token, or a periodic sweep
    racing the immediate best-effort send right after enqueue -- from both
    calling Meta for the SAME row (see module docstring)."""
    claim = db.execute(
        update(OutboundMessageDB)
        .where(OutboundMessageDB.id == row.id)
        .where(OutboundMessageDB.status.in_([OutboundMessageStatus.PENDING, OutboundMessageStatus.FAILED_RETRYABLE]))
        .values(
            status=OutboundMessageStatus.SENDING,
            attempt_count=OutboundMessageDB.attempt_count + 1,
            updated_at=timeutils.utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if claim.rowcount != 1:
        # Lost the claim race, or the row was no longer in a claimable state
        # by the time we got here -- never call Meta for a row this attempt
        # doesn't durably own.
        return
    db.refresh(row)  # pick up the status/attempt_count our own UPDATE just wrote

    try:
        wamid = adapter.send_message_with_id(row.whatsapp_id, row.body_text)
    except MetaApiError as exc:
        row.last_error = f"{type(exc).__name__}"
        if row.attempt_count >= settings.WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS:
            row.status = OutboundMessageStatus.FAILED_TERMINAL
            row.updated_at = timeutils.utcnow()
            log_security_event(
                db,
                action=SecurityEventType.VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL,
                actor_user_id=None,
                target_type="outbound_message",
                target_id=row.id,
                reason=f"exhausted {row.attempt_count} attempts",
            )
            logger.error("VIP_OUTBOUND_FAILED_TERMINAL id=%s attempts=%s", row.id, row.attempt_count)
        else:
            row.status = OutboundMessageStatus.FAILED_RETRYABLE
            row.next_attempt_at = timeutils.utcnow() + timedelta(seconds=_backoff_seconds(row.attempt_count))
            row.updated_at = timeutils.utcnow()
            logger.warning("VIP_OUTBOUND_RETRY id=%s attempt=%s", row.id, row.attempt_count)
        db.commit()
        return

    row.status = OutboundMessageStatus.META_ACCEPTED
    row.wamid = wamid
    row.updated_at = timeutils.utcnow()
    db.commit()


def reconcile_stuck_sending(db: Session, stuck_seconds: int) -> int:
    """Never retries these -- moves them to UNCERTAIN_NEEDS_RECONCILIATION
    and alerts (log + security event) so an operator resolves them
    explicitly. See module docstring for why a blind retry here is unsafe."""
    threshold = timeutils.utcnow() - timedelta(seconds=stuck_seconds)
    stuck = db.query(OutboundMessageDB).filter(OutboundMessageDB.status == OutboundMessageStatus.SENDING, OutboundMessageDB.updated_at < threshold).all()
    for row in stuck:
        row.status = OutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION
        row.updated_at = timeutils.utcnow()
        log_security_event(
            db,
            action=SecurityEventType.VIP_OUTBOUND_DELIVERY_UNCERTAIN,
            actor_user_id=None,
            target_type="outbound_message",
            target_id=row.id,
            reason="stuck in SENDING past the stuck-detection window -- outcome unknown, needs manual reconciliation",
        )
        logger.error("VIP_OUTBOUND_UNCERTAIN id=%s -- needs manual reconciliation, never auto-retried", row.id)
    if stuck:
        db.commit()
    return len(stuck)


def flush_pending_outbound(db: Session, adapter: MetaClient, *, limit: int = 20) -> dict:
    """The single entry point both the startup recovery sweep and the
    periodic loop call. Safe to call from multiple processes/ticks --
    acquire_lease() ensures only one actually does work per call when more
    than one races in. Returns a small summary dict (never raises).

    Uses THIS PROCESS's stable worker token (_PROCESS_WORKER_TOKEN), not a
    fresh random one per call -- so repeated ticks from the SAME process
    (the normal periodic-loop case) always renew their own still-valid
    lease instead of needlessly losing the CAS to themselves and skipping a
    sweep until the old lease naturally expires."""
    if not acquire_lease(db, _PROCESS_WORKER_TOKEN, settings.WHATSAPP_VIP_OUTBOX_LEASE_SECONDS):
        return {"lease_acquired": False}

    reconciled = reconcile_stuck_sending(db, settings.WHATSAPP_VIP_OUTBOX_STUCK_SECONDS)

    now = timeutils.utcnow()
    due = (
        db.query(OutboundMessageDB)
        .filter(
            or_(
                OutboundMessageDB.status == OutboundMessageStatus.PENDING,
                (OutboundMessageDB.status == OutboundMessageStatus.FAILED_RETRYABLE) & (OutboundMessageDB.next_attempt_at <= now),
            )
        )
        .order_by(OutboundMessageDB.id)
        .limit(limit)
        .all()
    )
    for row in due:
        attempt_send_one(db, row, adapter)

    return {"lease_acquired": True, "attempted": len(due), "reconciled_uncertain": reconciled}


# ---------------------------------------------------------------------------
# Manual reconciliation -- operator-invoked only (e.g. via a future admin
# script/route, not built in V1), never called automatically.
# ---------------------------------------------------------------------------


def list_uncertain(db: Session) -> list[OutboundMessageDB]:
    return db.query(OutboundMessageDB).filter(OutboundMessageDB.status == OutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION).order_by(OutboundMessageDB.id).all()


def mark_confirmed_sent(db: Session, row: OutboundMessageDB, *, wamid: str | None = None) -> OutboundMessageDB:
    """Operator verified out-of-band (checked Meta's message log, or the
    recipient confirmed receipt) that this attempt DID go through."""
    row.status = OutboundMessageStatus.META_ACCEPTED
    if wamid:
        row.wamid = wamid
    row.updated_at = timeutils.utcnow()
    db.commit()
    db.refresh(row)
    return row


_DELIVERED_STATUSES = {"delivered", "read"}
_FAILED_STATUSES = {"failed"}


def apply_status_update(db: Session, wamid: str, status: str) -> bool:
    """Best-effort correlation of a real Meta delivery-status webhook
    (sent/delivered/read/failed) with the outbox row that has this wamid.
    Returns False (no-op) if no matching row exists -- e.g. the status is
    for a message sent by a different flow, or from before this pilot
    existed; never an error. Only ever moves a row INTO DELIVERED/FAILED_TERMINAL
    from META_ACCEPTED -- never touches a row already in a terminal-ish or
    uncertain state, and never marks DELIVERED just because we once got a
    2xx from the send call (see module docstring)."""
    row = db.query(OutboundMessageDB).filter(OutboundMessageDB.wamid == wamid).first()
    if row is None or row.status != OutboundMessageStatus.META_ACCEPTED:
        return False
    if status in _DELIVERED_STATUSES:
        row.status = OutboundMessageStatus.DELIVERED
    elif status in _FAILED_STATUSES:
        row.status = OutboundMessageStatus.FAILED_TERMINAL
        row.last_error = "meta_status_webhook_failed"
    else:
        return False  # "sent" or an unrecognized status -- no state change needed
    row.updated_at = timeutils.utcnow()
    db.commit()
    return True


def mark_confirmed_not_sent(db: Session, row: OutboundMessageDB) -> OutboundMessageDB:
    """Operator verified out-of-band that this attempt did NOT go through --
    safe to requeue for a real retry now that the ambiguity is resolved."""
    row.status = OutboundMessageStatus.PENDING
    row.next_attempt_at = None
    row.updated_at = timeutils.utcnow()
    db.commit()
    db.refresh(row)
    return row
