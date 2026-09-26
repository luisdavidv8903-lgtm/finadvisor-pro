# WhatsApp wallet-link: atomic consumption and audit

Scope: local source only, before commit. No VPS, Meta, deployments, contracts,
secrets, or runtime configuration changed. WALLET_HANDOFF_ENABLED stays false.

## Baseline

Input snapshot: chambeando_audit_snapshot.zip

SHA-256: 47eb706156a2f061d534cace408d9990468bc5f60e657957b15d93881955f14b

The input hash matches its sidecar. Before this patch the source files matched
the snapshot; MANIFEST.txt is a generated snapshot artifact, not a source file.
Existing uncommitted multi-network changes are preserved. The delivered patch
is relative to this snapshot, not to Git HEAD.

## Consumption

consume_link_token preserves the existing error checks, then performs a SQL
UPDATE guarded by row ID, current token hash, non-null/unexpired deadline,
issued network, and an owner that is either absent or the authenticated user.
It checks rowcount == 1; zero rows is a rejected consume. Clearing the token
and assigning the owner happen in that same UPDATE. The helper refreshes the
ORM instance but DOES NOT COMMIT. Direct callers must commit or roll back.
The only production caller is /whatsapp/link.

Legacy callers without a network argument retain their existing behavior;
the SQL predicate still guards the issued network against a concurrent change.
The HTTP endpoint continues requiring JWT network == request network == issued
network. TRON and auth-only EVM behavior are preserved.

## Transaction boundary

/whatsapp/link owns one transaction for wallet assignment, token consumption,
and WHATSAPP_ACCOUNT_LINKED. Audit flush or commit failure rolls it all back.
Notification runs only after commit. A later notification failure does not
undo an already committed link/audit pair; no notification delivery guarantee
or transactional outbox is added by this patch.

## Issuance concurrency review

There is one outstanding token slot per WhatsApp identity. Explicit subsequent
issuance intentionally invalidates the previous unconsumed token. It never
changes an existing wallet owner. Reissuing after linking still permits the
same owner to link again and still rejects a different wallet.

Previously an issuer could read a row, a consumer could commit, and the issuer
could then write new token fields based on its stale read. Issuance now uses a
compare-and-swap over the observed hash, expiry, issued network and owner.
A stale issuer fails with LinkTokenError and must retry from fresh state; it
cannot silently resurrect token state after a concurrent consume/reissue.
Conversely, when issuance commits first, consumption of the old token fails
its guarded UPDATE. A committed wallet link cannot be reassigned this way.

Concurrent first-time creation still uses the existing unique whatsapp_id
constraint/get_or_create behavior; this patch does not add retries for its
possible insert conflict. No lost-update success is claimed for that case.

## Validation

test_wallet_link_atomicity.py uses real independent database connections:

- Two threads read the same token before either writes, then compete with
  different wallet owners. Exactly one commits and one receives LinkTokenError;
  the persisted owner must equal the winner. Tested for TRON and EVM.
- A real NOT NULL violation on security-event INSERT rolls back owner and token
  changes. A separate session verifies the original token fields, then the same
  token is retried successfully with exactly one audit event.
- HTTP /whatsapp/link exercises the same failure and successful retry.
- Concurrent changes to hash, expiry, network or owner reject a stale consumer.
- A stale issuer cannot resurrect token state; deliberate reissue rotates the
  old token and does not bypass the existing-owner policy.

SQLite uses file-backed databases. PostgreSQL variants use a unique disposable
schema in POSTGRES_TEST_DATABASE_URL, not TRUNCATE of application tables.
The configured local chambeando_test server was unavailable (OperationalError),
so PostgreSQL cases were not executed. No server was started or reconfigured.

The first regression run passed 62 tests and skipped 5 PostgreSQL cases.
An expanded run exposed a too-short synthetic wallet in the new HTTP test
(422 before linking); its test data was corrected. See tests-final.txt in the
audit artifacts for the final combined regression result and warnings.

Final combined run: **107 passed, 0 failed, 9 skipped, 2 warnings in 71.85s**.
All skips are PostgreSQL variants; warnings are TestClient/AnyIO deprecations.
The interrupted task resumed on 2026-09-26: source reviewed, results recovered,
and tests enumerated with --collect-only (not rerun). See tests-results.tsv
for each exact test identifier and status, derived from the completed run
and its matching collection. git diff --check passed.

The changes are applied in the real Desktop/finadvisor-pro/chambeando repo,
not merely in the audit artifact directory. No commit or push was made.

## Snapshot verification

The new ZIP preserves every baseline member except the two corrected source
files and the regenerated MANIFEST.txt, and adds this report and the new tests.
MANIFEST.txt lists SHA-256 for every other ZIP member (it excludes itself).
The external .sha256 file authenticates the entire ZIP. The snapshot excludes
live environment files, databases, runtime logs, dependencies and private keys
by retaining the audited baseline's explicit file set.
