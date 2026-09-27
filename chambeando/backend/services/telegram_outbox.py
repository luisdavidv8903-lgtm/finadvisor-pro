"""
Telegram VIP outbox -- durable outbound delivery for telegram_vip_router.py.

Added 2026-09-27 after an audit ("revision final del piloto") proved the
pilot's original design lost replies: routers/telegram.py committed the
wizard/session-state advance and the webhook idempotency marker, THEN
called TelegramAdapter.send() in a plain loop over an in-memory list
returned by handle_inbound(). TelegramAdapter.send() swallows
TelegramApiError (same best-effort contract as WhatsAppAdapter.send, so the
inbound webhook's ack never depends on outbound success) -- which means a
transient Telegram API failure after the commit silently drops the reply
forever, with NO durable record it was ever owed. Worse: if the process
crashes between the commit succeeding and that send loop running, the
in-memory pending_sends list is gone with it -- nothing was ever persisted,
so no restart/recovery sweep can find it. See
tests/test_telegram_vip_outbox_reliability.py for the tests that
demonstrate both failure modes against the pre-fix code and prove this
module closes them.

State machine (TelegramOutboundMessageStatus) -- deliberately simpler than
OutboundMessageStatus (services/whatsapp_outbox.py), because Telegram's Bot
API sendMessage call IS the definitive synchronous outcome: a 2xx response
means Telegram accepted and queued the message for real delivery, and
there is no later asynchronous "delivered" webhook this pilot correlates
against (Telegram does offer delivery/read receipts for some update types,
but not for a plain bot sendMessage in a private chat) -- so SENT is the
terminal success state, not a "we THINK it went out" intermediate one:

    PENDING ---------------> SENDING ---------------------------> SENT
       ^                        |    \\        \\
       |                        |     \\        \\ (429/5xx -- transient,
       | (operator confirms     |      \\        \\  received, bounded retry)
       |  "never actually       |       \\        v
       |   sent" after an       |        \\    FAILED_RETRYABLE -> FAILED_TERMINAL
       |   UNCERTAIN row)       |         \\      (attempts exhausted)
       |                        |          v
       |                        |       FAILED_PERMANENT (400/401/403 --
       |                        |        received, known non-retryable,
       |                        |        reached on the FIRST such response)
       |                        v (EITHER: stuck > STUCK_SECONDS, process
       |                           likely died mid-call -- OR: a timeout/
       |                           connection-lost with NO response ever
       |                           received -- both outcomes unknown)
       +-------------------- UNCERTAIN_NEEDS_RECONCILIATION

Revision 2026-09-27 ("cierra dos riesgos", second pass -- "ultima
correccion"): attempt_send_one now distinguishes THREE outcomes, using
three distinct exception types from messaging/telegram_adapter.py, never
assuming every non-2xx status in a range shares one cause:

  - TelegramApiUncertainError (timeout / connection lost -- NO response was
    ever received): straight to UNCERTAIN_NEEDS_RECONCILIATION, exactly as
    the first pass already established. See
    test_the_fix_recovers_reply_after_api_failure_then_sweep_retry /
    test_timeout_after_send_produces_uncertain_never_failed_retryable.
  - TelegramApiPermanentError (a REAL, received HTTP 400/401/403, or any
    other 4xx except 429): a request that failed for a reason retrying the
    SAME request cannot fix. Goes DIRECTLY to FAILED_PERMANENT on the
    first such response -- no attempt budget spent retrying something we
    already know will never work -- with a dedicated log line + security
    event as the operational signal. See
    test_permanent_rejection_never_retries_and_signals_operator.
  - TelegramApiTransientError (a REAL, received HTTP 429 or any 5xx): a
    request that MAY succeed if retried once the condition clears. Uses
    the existing bounded retry-with-backoff path (FAILED_RETRYABLE ->
    FAILED_TERMINAL once WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS is exhausted --
    same finite budget as every other retryable failure, so this can never
    loop forever), honoring Telegram's own `retry_after` (from the
    response body's `parameters.retry_after` or a `Retry-After` header)
    as the MINIMUM backoff when Telegram supplies one, always capped by
    _backoff_seconds. See
    test_429_respects_retry_after_and_retries_bounded and
    test_5xx_is_treated_as_transient_and_retried.

THE OTHER UNCERTAINTY WINDOW (unchanged, same shape as WhatsApp's): between
"we called TelegramClient.send_message()" and "we recorded an outcome for
that attempt" (i.e. a crash/OOM/systemd-restart genuinely mid-call, before
even an exception could be raised and caught), a row stuck in SENDING past
the stuck-detection window is moved to UNCERTAIN_NEEDS_RECONCILIATION by
reconcile_stuck_sending. Both paths converge on the SAME terminal-for-
automation state and the SAME manual-resolution procedure (see the
"Manual reconciliation" section below) -- NEVER auto-retried (a blind retry
here risks a real duplicate message to a real person, exactly the
"reintentos ciegos" the task explicitly forbids) -- an operator must check
and call mark_confirmed_sent()/mark_confirmed_not_sent() explicitly. This
module never asserts "delivered exactly once" for either kind of
UNCERTAIN row -- only that it is durably queued for a human to resolve.

Tuning reuses the SAME settings fields as the WhatsApp outbox
(WHATSAPP_VIP_OUTBOX_POLL_SECONDS/MAX_ATTEMPTS/STUCK_SECONDS/LEASE_SECONDS/
AUTOSTART) rather than adding a parallel set of Telegram-specific knobs --
this is one small pilot, not two independently-tuned subsystems, and the
two outboxes' reliability requirements are identical. The lease
(OutboxWorkerLeaseDB) is likewise the SAME table as WhatsApp's, just a
different row id (2, vs WhatsApp's 1) -- see that model's docstring.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .. import timeutils
from ..config import settings
from ..messaging.telegram_adapter import TelegramApiError, TelegramApiPermanentError, TelegramApiTransientError, TelegramApiUncertainError, TelegramClient
from ..models import OutboxWorkerLeaseDB, SecurityEventType, TelegramOutboundMessageDB, TelegramOutboundMessageStatus
from ..security.audit import log_security_event

logger = logging.getLogger("chambeando.telegram_vip.outbox")

_LEASE_ROW_ID = 2  # id=1 is the WhatsApp VIP outbox's lease -- see OutboxWorkerLeaseDB's docstring
_PROCESS_WORKER_TOKEN = uuid.uuid4().hex


def enqueue(db: Session, telegram_chat_id: str, text: str, *, source_message_id: str | None = None) -> TelegramOutboundMessageDB:
    """Standalone durable enqueue -- commits immediately. For callers outside
    the VIP inbound webhook path (tests, the process-restart recovery path,
    or any future direct caller). telegram_vip_router.py's inbound handling
    does NOT use this directly -- see stage_pending below."""
    row = stage_pending(db, telegram_chat_id, text, source_message_id=source_message_id)
    db.commit()
    db.refresh(row)
    return row


def stage_pending(db: Session, telegram_chat_id: str, text: str, *, source_message_id: str | None = None) -> TelegramOutboundMessageDB:
    """Adds a PENDING row to the session WITHOUT committing -- the caller
    (telegram_vip_router.py's _send) is responsible for committing it as
    part of its own atomic unit, together with the wizard's session-state
    advance and the webhook idempotency marker (routers/telegram.py's
    _handle_telegram_vip_message_atomically). db.flush() (not commit) so
    row.id is available right away without ending the transaction."""
    row = TelegramOutboundMessageDB(telegram_chat_id=telegram_chat_id, body_text=text, source_message_id=source_message_id)
    db.add(row)
    db.flush()
    return row


def _ensure_lease_row(db: Session) -> None:
    if db.query(OutboxWorkerLeaseDB).filter(OutboxWorkerLeaseDB.id == _LEASE_ROW_ID).first() is not None:
        return
    db.add(OutboxWorkerLeaseDB(id=_LEASE_ROW_ID, owner_token=None, lease_expires_at=None))
    try:
        db.commit()
    except Exception:  # another worker raced us to create this lease row -- fine either way
        db.rollback()


def acquire_lease(db: Session, worker_token: str, lease_seconds: int) -> bool:
    """Atomic compare-and-swap, identical shape to
    services/whatsapp_outbox.py's acquire_lease -- see that function's
    docstring for the synchronize_session=False rationale (naive-vs-aware
    datetime comparison under SQLite)."""
    _ensure_lease_row(db)
    now = timeutils.utcnow()
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


def attempt_send_one(db: Session, row: TelegramOutboundMessageDB, client: TelegramClient) -> None:
    """Marks SENDING durably BEFORE calling the Telegram Bot API (so a crash
    mid-call leaves unambiguous evidence an attempt was in flight), then
    records exactly one of: SENT, FAILED_RETRYABLE, FAILED_TERMINAL,
    FAILED_PERMANENT, or UNCERTAIN_NEEDS_RECONCILIATION.

    Revision 2026-09-27, second pass ("ultima correccion antes de cerrar un
    commit local"): three distinct exception types from
    messaging/telegram_adapter.py are handled as three distinct outcomes,
    never lumped together:

      - TelegramApiUncertainError (no response ever received -- timeout or
        connection lost): straight to UNCERTAIN_NEEDS_RECONCILIATION,
        never touches the retry budget. Same reasoning as
        reconcile_stuck_sending's crash-recovery path below, and the same
        manual-resolution procedure (mark_confirmed_sent/
        mark_confirmed_not_sent) applies to either kind of UNCERTAIN row.
      - TelegramApiPermanentError (a REAL response: HTTP 400/401/403, or
        any other 4xx except 429): we KNOW retrying the identical request
        cannot succeed -- straight to FAILED_PERMANENT on the FIRST such
        response, with its own operational signal (log line + security
        event), never spending a single retry attempt on it.
      - TelegramApiTransientError (a REAL response: HTTP 429 or any 5xx):
        may succeed on retry once the condition clears -- uses the
        existing bounded FAILED_RETRYABLE -> FAILED_TERMINAL path, honoring
        Telegram's own `retry_after` as a floor on the backoff when
        supplied, always capped by _backoff_seconds so this can never
        stall indefinitely.

    Whichever branch is taken, this module NEVER writes "successfully
    delivered" or "delivered to exactly one recipient" for an UNCERTAIN
    row -- SENT is set ONLY when a 2xx response was actually received (see
    the `try` block below); an ambiguous outcome stays ambiguous until an
    operator resolves it. FAILED_PERMANENT is equally final but for the
    OPPOSITE reason: not because the outcome is unknown, but because it is
    known with certainty to be un-retriable.

    The transition into SENDING is an ATOMIC compare-and-swap, identical
    shape to services/whatsapp_outbox.py's attempt_send_one -- the UPDATE's
    WHERE clause re-checks the row is still PENDING/FAILED_RETRYABLE in the
    SAME statement the database executes, so two overlapping sweeps (or a
    sweep racing the immediate best-effort send right after enqueue) can
    never both call the Telegram API for the SAME row."""
    claim = db.execute(
        update(TelegramOutboundMessageDB)
        .where(TelegramOutboundMessageDB.id == row.id)
        .where(TelegramOutboundMessageDB.status.in_([TelegramOutboundMessageStatus.PENDING, TelegramOutboundMessageStatus.FAILED_RETRYABLE]))
        .values(
            status=TelegramOutboundMessageStatus.SENDING,
            attempt_count=TelegramOutboundMessageDB.attempt_count + 1,
            updated_at=timeutils.utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if claim.rowcount != 1:
        # Lost the claim race, or the row was no longer in a claimable state
        # by the time we got here -- never call the Telegram API for a row
        # this attempt doesn't durably own.
        return
    db.refresh(row)  # pick up the status/attempt_count our own UPDATE just wrote

    try:
        client.send_message(row.telegram_chat_id, row.body_text)
    except TelegramApiUncertainError as exc:
        # Ambiguous outcome -- no response was ever received, so Telegram
        # may already have sent this message. NEVER counted as a "clean"
        # failure and NEVER retried automatically: straight to
        # UNCERTAIN_NEEDS_RECONCILIATION, same as reconcile_stuck_sending's
        # own crash-recovery path. attempt_count was already incremented by
        # the claim UPDATE above -- left as-is, purely informational here
        # (an UNCERTAIN row is never re-claimed by the PENDING/
        # FAILED_RETRYABLE filter attempt_send_one's own claim query uses).
        row.last_error = f"{type(exc).__name__}"
        row.status = TelegramOutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION
        row.updated_at = timeutils.utcnow()
        log_security_event(
            db,
            action=SecurityEventType.TELEGRAM_VIP_OUTBOUND_DELIVERY_UNCERTAIN,
            actor_user_id=None,
            target_type="telegram_outbound_message",
            target_id=row.id,
            reason="no response received (timeout/connection lost) -- outcome unknown, needs manual reconciliation",
        )
        logger.error("TELEGRAM_VIP_OUTBOUND_UNCERTAIN id=%s -- no response received, needs manual reconciliation, never auto-retried", row.id)
        db.commit()
        return
    except TelegramApiPermanentError as exc:
        # A REAL response was received and it is a known-permanent
        # rejection -- retrying the identical request cannot succeed.
        # Never spends a retry attempt on it; moves straight to
        # FAILED_PERMANENT with its own operational signal.
        row.last_error = f"{type(exc).__name__} status={exc.status_code} error_code={exc.error_code}"
        row.status = TelegramOutboundMessageStatus.FAILED_PERMANENT
        row.updated_at = timeutils.utcnow()
        log_security_event(
            db,
            action=SecurityEventType.TELEGRAM_VIP_OUTBOUND_DELIVERY_FAILED_PERMANENT,
            actor_user_id=None,
            target_type="telegram_outbound_message",
            target_id=row.id,
            reason=f"permanent rejection status={exc.status_code} error_code={exc.error_code}",
        )
        logger.error("TELEGRAM_VIP_OUTBOUND_FAILED_PERMANENT id=%s status=%s error_code=%s -- never retried", row.id, exc.status_code, exc.error_code)
        db.commit()
        return
    except TelegramApiTransientError as exc:
        # A REAL response was received and it indicates a condition that
        # may clear with time (rate limit, server error). Safe to count
        # toward the retry budget -- same finite WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS
        # cap as any other retryable failure, so this can never loop
        # forever.
        row.last_error = f"{type(exc).__name__} status={exc.status_code} error_code={exc.error_code}"
        if row.attempt_count >= settings.WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS:
            row.status = TelegramOutboundMessageStatus.FAILED_TERMINAL
            row.updated_at = timeutils.utcnow()
            log_security_event(
                db,
                action=SecurityEventType.TELEGRAM_VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL,
                actor_user_id=None,
                target_type="telegram_outbound_message",
                target_id=row.id,
                reason=f"exhausted {row.attempt_count} attempts (status={exc.status_code})",
            )
            logger.error("TELEGRAM_VIP_OUTBOUND_FAILED_TERMINAL id=%s attempts=%s status=%s", row.id, row.attempt_count, exc.status_code)
        else:
            row.status = TelegramOutboundMessageStatus.FAILED_RETRYABLE
            backoff = _backoff_seconds(row.attempt_count)
            if exc.retry_after is not None:
                # Honor Telegram's own requested backoff as a FLOOR, never
                # shorter than what it asked for -- but still capped by
                # _backoff_seconds' own ceiling, so a single very large
                # value can never stall reconciliation indefinitely.
                backoff = max(backoff, min(exc.retry_after, 300))
            row.next_attempt_at = timeutils.utcnow() + timedelta(seconds=backoff)
            row.updated_at = timeutils.utcnow()
            logger.warning("TELEGRAM_VIP_OUTBOUND_RETRY id=%s attempt=%s status=%s backoff=%s", row.id, row.attempt_count, exc.status_code, backoff)
        db.commit()
        return
    except TelegramApiError as exc:
        # Fallback for a genuinely unclassified TelegramApiError -- never
        # raised by RealTelegramClient itself (every real path above
        # raises one of the three specific subclasses), but a test double
        # or a future client implementation could raise the bare base
        # class. Treated conservatively via the same bounded retry path as
        # a transient failure, rather than crashing the sweep or silently
        # dropping it.
        row.last_error = f"{type(exc).__name__}"
        if row.attempt_count >= settings.WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS:
            row.status = TelegramOutboundMessageStatus.FAILED_TERMINAL
            row.updated_at = timeutils.utcnow()
            log_security_event(
                db,
                action=SecurityEventType.TELEGRAM_VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL,
                actor_user_id=None,
                target_type="telegram_outbound_message",
                target_id=row.id,
                reason=f"exhausted {row.attempt_count} attempts (unclassified error)",
            )
            logger.error("TELEGRAM_VIP_OUTBOUND_FAILED_TERMINAL id=%s attempts=%s (unclassified)", row.id, row.attempt_count)
        else:
            row.status = TelegramOutboundMessageStatus.FAILED_RETRYABLE
            row.next_attempt_at = timeutils.utcnow() + timedelta(seconds=_backoff_seconds(row.attempt_count))
            row.updated_at = timeutils.utcnow()
            logger.warning("TELEGRAM_VIP_OUTBOUND_RETRY id=%s attempt=%s (unclassified)", row.id, row.attempt_count)
        db.commit()
        return

    row.status = TelegramOutboundMessageStatus.SENT
    row.updated_at = timeutils.utcnow()
    db.commit()


def reconcile_stuck_sending(db: Session, stuck_seconds: int) -> int:
    """Never retries these -- moves them to UNCERTAIN_NEEDS_RECONCILIATION
    and alerts (log + security event) so an operator resolves them
    explicitly. See module docstring for why a blind retry here is unsafe."""
    threshold = timeutils.utcnow() - timedelta(seconds=stuck_seconds)
    stuck = (
        db.query(TelegramOutboundMessageDB)
        .filter(TelegramOutboundMessageDB.status == TelegramOutboundMessageStatus.SENDING, TelegramOutboundMessageDB.updated_at < threshold)
        .all()
    )
    for row in stuck:
        row.status = TelegramOutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION
        row.updated_at = timeutils.utcnow()
        log_security_event(
            db,
            action=SecurityEventType.TELEGRAM_VIP_OUTBOUND_DELIVERY_UNCERTAIN,
            actor_user_id=None,
            target_type="telegram_outbound_message",
            target_id=row.id,
            reason="stuck in SENDING past the stuck-detection window -- outcome unknown, needs manual reconciliation",
        )
        logger.error("TELEGRAM_VIP_OUTBOUND_UNCERTAIN id=%s -- needs manual reconciliation, never auto-retried", row.id)
    if stuck:
        db.commit()
    return len(stuck)


def flush_pending_outbound(db: Session, client: TelegramClient, *, limit: int = 20) -> dict:
    """The single entry point both the startup recovery sweep and the
    periodic loop call -- identical shape to
    services/whatsapp_outbox.py's flush_pending_outbound. Safe to call from
    multiple processes/ticks: acquire_lease() ensures only one actually does
    work per call when more than one races in."""
    if not acquire_lease(db, _PROCESS_WORKER_TOKEN, settings.WHATSAPP_VIP_OUTBOX_LEASE_SECONDS):
        return {"lease_acquired": False}

    reconciled = reconcile_stuck_sending(db, settings.WHATSAPP_VIP_OUTBOX_STUCK_SECONDS)

    now = timeutils.utcnow()
    due = (
        db.query(TelegramOutboundMessageDB)
        .filter(
            or_(
                TelegramOutboundMessageDB.status == TelegramOutboundMessageStatus.PENDING,
                (TelegramOutboundMessageDB.status == TelegramOutboundMessageStatus.FAILED_RETRYABLE) & (TelegramOutboundMessageDB.next_attempt_at <= now),
            )
        )
        .order_by(TelegramOutboundMessageDB.id)
        .limit(limit)
        .all()
    )
    for row in due:
        attempt_send_one(db, row, client)

    return {"lease_acquired": True, "attempted": len(due), "reconciled_uncertain": reconciled}


# ---------------------------------------------------------------------------
# Manual reconciliation -- operator-invoked only, never called automatically.
#
# HOW AN OPERATOR RESOLVES AN UNCERTAIN ROW, without ever revealing
# body_text (the VIP reply's content) or telegram_chat_id (the recipient's
# numeric Telegram id) in a log, chat message, or ticket:
#
#   1. List the queue: `telegram_outbox.list_uncertain(db)`. For each row,
#      note ONLY its id, created_at, and attempt_count -- these are
#      metadata, never sensitive (same "log WHO/WHAT/WHEN, never the VALUE"
#      discipline as security/audit.py elsewhere in this codebase). Do NOT
#      print row.body_text or row.telegram_chat_id to a terminal, log, or
#      any channel a second person might read.
#   2. Check Telegram's own record of what was actually sent, using the
#      bot's OWN chat history via the Bot API's getUpdates/getChat, or
#      (simplest and this pilot's intended path) by asking the recipient
#      directly: "¿te llego un mensaje del bot sobre la oferta a las
#      <created_at>?" -- a yes/no question that never requires repeating
#      the message content back to them, since they already have it if it
#      arrived.
#   3. If confirmed delivered: `mark_confirmed_sent(db, row)`. If confirmed
#      NOT delivered: `mark_confirmed_not_sent(db, row)`, which safely
#      requeues it for a real retry -- safe specifically because the
#      ambiguity is now resolved by an out-of-band human check, not by
#      this module guessing.
#   4. Never call either function on a hunch -- an UNCERTAIN row is
#      deliberately inert (excluded from every automatic claim/retry query
#      in this module) until a human resolves it explicitly, which is the
#      entire point of this state existing.
# ---------------------------------------------------------------------------


def list_uncertain(db: Session) -> list[TelegramOutboundMessageDB]:
    """Returns full rows (SQLAlchemy objects), but see the reconciliation
    procedure above: a caller building an operator-facing listing/report
    from this should surface only id/created_at/attempt_count -- never
    row.body_text or row.telegram_chat_id."""
    return (
        db.query(TelegramOutboundMessageDB)
        .filter(TelegramOutboundMessageDB.status == TelegramOutboundMessageStatus.UNCERTAIN_NEEDS_RECONCILIATION)
        .order_by(TelegramOutboundMessageDB.id)
        .all()
    )


def mark_confirmed_sent(db: Session, row: TelegramOutboundMessageDB) -> TelegramOutboundMessageDB:
    """Operator verified out-of-band that this attempt DID go through."""
    row.status = TelegramOutboundMessageStatus.SENT
    row.updated_at = timeutils.utcnow()
    db.commit()
    db.refresh(row)
    return row


def mark_confirmed_not_sent(db: Session, row: TelegramOutboundMessageDB) -> TelegramOutboundMessageDB:
    """Operator verified out-of-band that this attempt did NOT go through --
    safe to requeue for a real retry now that the ambiguity is resolved."""
    row.status = TelegramOutboundMessageStatus.PENDING
    row.next_attempt_at = None
    row.updated_at = timeutils.utcnow()
    db.commit()
    db.refresh(row)
    return row
