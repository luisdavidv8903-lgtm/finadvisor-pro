# Settlement Detail Lifecycle — Phase 2B.2 Re-Audit

## The four questions, answered explicitly

**Can owner revoke settlement details after buyer has matched?**
Yes, and this is unchanged — `POST /settlement-details/{id}/revoke` has no check preventing revocation of a "master" record that's linked to an active/matched order. This is intentional: revocation is a statement about the master record going forward (e.g., "don't use this bank account for *new* trades"), not an attempt to reach into trades that already captured it.

**What does the matched counterparty see if the record is revoked mid-trade?**
**Before this phase (the bug):** a 404 — the counterparty lost access to the payment instructions they needed to complete or verify the trade, mid-trade, because `reveal_order_settlement` did a live lookup against the (now-inactive) master `SettlementDetailDB` row.
**After this phase (the fix):** the counterparty still sees the exact same instructions they always could. `P2POrderDB` now carries its own `settlement_snapshot_payload` (and `settlement_snapshot_payment_method`), copied from the master record once, at `attach_order_metadata()` time, in the same transaction as the rest of that call. `reveal_order_settlement` reads *only* the snapshot — it no longer touches the live master record at all.

**Are settlement instructions immutable for the lifetime of that trade?**
Yes, and this was already true for content (no `PUT`/`PATCH` endpoint on `SettlementDetailDB` ever existed — `test_settlement_payload_immutable_once_snapshotted_even_if_master_record_survives` confirms no in-place reassignment exists in the router source), but was **not** true for *availability* before this fix (revocation could make it inaccessible). Now both content and availability are guaranteed for the life of the trade.

**Can linking another settlement record silently alter an existing matched trade?**
No. `attach_order_metadata` is write-once per order — it explicitly rejects a second call (`if order.fiat_amount is not None: raise HTTPException(400, "ya tiene metadata adjunta")`), before ever looking at `settlement_detail_id`. There is no endpoint that re-links or replaces `settlement_detail_id` on an order that already has metadata. `test_attempted_post_match_replacement_is_rejected` proves a second attach attempt with a *different* `settlement_detail_id` is rejected outright and the original snapshot is untouched.

## The mechanism (implemented this phase)

`P2POrderDB` gained two columns:
- `settlement_snapshot_payload` (`LargeBinary`, nullable) — a byte-for-byte copy of the master `SettlementDetailDB.encrypted_payload` at the moment of linking. It's still ciphertext (same key, same Fernet token) — copying it doesn't re-encrypt or weaken anything, it just duplicates the already-encrypted value so this order no longer depends on the master row's future state.
- `settlement_snapshot_payment_method` (`String`, nullable) — same idea, for the non-sensitive display field.

`settlement_detail_id` is kept as-is, purely for traceability ("which master record did this originate from") — it is no longer read by `reveal_order_settlement`.

## Why snapshot at attach-time rather than at match-time

The order's match status (`OPEN`→`CLAIMED`) is entirely on-chain, indexer-owned, and can occur before *or* after the seller calls `attach_order_metadata`. Rather than adding logic to detect "did a match happen since this was linked" and conditionally snapshot, snapshotting unconditionally at attach-time is simpler and strictly safer: the guarantee holds from the moment metadata exists, not just from the moment a match happens to have occurred by then.

## What this does NOT change

- Settlement data is still never written on-chain, still never in logs (verified by existing `test_logging.py`/`test_settlement_security.py` tests), still encrypted at rest with the same Fernet scheme from Phase 2B.
- Creating, listing, and revoking a user's own settlement details (the "master" records) works exactly as before — this phase only changed what `GET /orders/{id}/settlement` reads from.
