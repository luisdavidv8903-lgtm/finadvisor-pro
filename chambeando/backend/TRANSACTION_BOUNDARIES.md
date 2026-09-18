# Transaction Boundary Audit — updated Phase 2B.3

Phase 2B.2's audit found a recurring gap: every privileged action's primary write and its `SecurityEventDB` audit event were **two separate commits**. If the second (audit) commit failed after the first (primary write) had already succeeded, the privileged action survived without its audit trail. **Phase 2B.3 fixed this** — `security/audit.log_security_event()` no longer commits (`db.add(event); db.flush()` only); every caller now performs exactly one `db.commit()` that covers both the primary write and the audit event together. This doc reflects the fixed state; see git history / the Phase 2B.2 report for the prior (broken) shape.

## 1. Invite redemption + membership creation (`services/invites.py::redeem_invite_for_user`)

```
BEGIN (implicit, session already open)
  READ:   does a MembershipDB already exist for this user? -> if yes, raise before touching anything else
  READ:   fetch InviteDB by code_hash
  WRITE:  UPDATE invites SET used_count = used_count + 1
          WHERE id=? AND revoked_at IS NULL AND expires_at >= now AND used_count < max_uses
          (atomic compare-and-swap; rowcount==0 -> ROLLBACK, raise, nothing persisted)
  WRITE:  INSERT membership (user_id, invited_by_user_id, invite_id)
  WRITE:  log_security_event(INVITE_REDEEMED) — add + flush, same transaction (Phase 2B.3)
COMMIT — invite consumption + membership + audit event, ALL together, one commit
  on IntegrityError (UniqueConstraint(user_id) race, OR now also a failed audit flush) ->
  ROLLBACK undoes the used_count increment, the membership insert, AND the audit event
```
**Strongest of the set** — proven under real concurrent OS threads (`tests/test_invite_atomicity.py`, Phase 2B.1) *and* under real audit-insert failure injection (`tests/test_atomic_audit_log.py::test_invite_redemption_rolls_back_if_audit_insert_fails`, Phase 2B.3).

## 2. Dispute assignment creation / revocation (`routers/admin.py`)

**Creation:** `READ` order is DISPUTED, `READ` target is MODERATOR/ADMIN → `INSERT` assignment → `flush` (populate id) → `log_security_event` (add + flush) → **one `COMMIT`**. Rollback verified for real (`test_dispute_assignment_creation_rolls_back_if_audit_insert_fails`).
**Revocation:** `READ` assignment → `WRITE` `revoked_at` → `log_security_event` → **one `COMMIT`**. Rollback verified (`test_dispute_assignment_revocation_rolls_back_if_audit_insert_fails`).

## 3. Settlement detail creation / revocation (`routers/settlement.py`)

**Creation:** encrypt payload (in-memory) → `INSERT` → `flush` → `log_security_event` → **one `COMMIT`**. Rollback verified (`test_settlement_creation_rolls_back_if_audit_insert_fails`) — no sensitive row survives a failed audit insert.
**Revocation:** `READ` detail → `WRITE` `active=False` → `log_security_event` → **one `COMMIT`**. Rollback verified.

## 4. Role changes (`routers/admin.py::assign_role`)

`READ` target → `WRITE` `role` → `log_security_event` (includes "old -> new" in `reason`) → **one `COMMIT`**. Rollback verified (`test_role_change_rolls_back_if_audit_insert_fails`).

## 5. Suspension / reactivation (`routers/admin.py`)

`READ` target → `WRITE` status/suspended fields → `log_security_event` → **one `COMMIT`**. Rollback verified for both directions (`test_suspend_membership_rolls_back_if_audit_insert_fails`, `test_reactivate_membership_rolls_back_if_audit_insert_fails`).

## 6. Invite creation / revocation (`routers/admin.py`)

**Creation:** `INSERT` invite → `flush` (populate id) → `log_security_event` → **one `COMMIT`**. Rollback verified.
**Revocation:** `WRITE` `revoked_at` → `log_security_event` → **one `COMMIT`**. Rollback verified.

## 7. Bootstrap ADMIN (`bootstrap_admin.py::bootstrap_admin`)

`INSERT` user → `flush` (populate id) → `INSERT` membership → `log_security_event` → **one `COMMIT`**, all wrapped in `try/except Exception: db.rollback(); raise`. Rollback verified — no admin user or membership survives a failed audit insert (`test_bootstrap_admin_rolls_back_if_audit_insert_fails`).

## 8. Report review (`routers/reports.py::review_report`)

Newly brought into scope this phase (privileged: MODERATOR/ADMIN-only, mutates persistent state) — a `REPORT_REVIEWED` audit event type was added. `READ` report → `WRITE` status/reviewed_by/reviewed_at → `log_security_event` → **one `COMMIT`**. Rollback verified.

## Also fixed for consistency (not in the original enumerated list, but the identical bug class): report filing (`routers/reports.py::file_report`) and dispute evidence submission (`routers/disputes.py::submit_evidence`) — both now single-commit with their audit event.

## View-only audit events (not part of the atomicity fix — nothing to be atomic *with*)

`AUTH_SUCCESS`/`AUTH_FAILURE` (`routers/auth.py`), `SETTLEMENT_DETAIL_VIEWED` (`routers/settlement.py`), `DISPUTE_EVIDENCE_VIEWED` (`routers/disputes.py`), and `INVITE_REDEMPTION_DENIED` (`routers/invites.py`) log an event with no accompanying primary mutation — there's no "primary write" for these to be atomic *with*. Each now simply gets its own `db.commit()` immediately after `log_security_event()` (previously implicit inside the helper; now explicit, since the helper no longer auto-commits). No atomicity property is claimed or needed here.

## Settlement snapshot at metadata attach (`routers/orders.py::attach_order_metadata`)

Unchanged from Phase 2B.2 — a single transaction, but genuinely doesn't log an audit event at all (metadata attachment isn't staff/privileged; the seller is managing their own listing). Still flagged as non-blocking technical debt if a future need arises to audit "when was settlement data attached to this order" for dispute investigation.

## Net result

There is no longer any privileged action in scope with "primary write committed, audit write pending separately." Every one of the 12 privileged actions the task enumerated (plus report review, added this phase) has a dedicated DB-level rollback test in `tests/test_atomic_audit_log.py`, using real `ForeignKey` constraint violations (not mocked exceptions) as the failure-injection mechanism — see `ENFORCEMENT_LEVELS.md` for how this changes the enforcement-level classification of "primary write + audit trail together."
