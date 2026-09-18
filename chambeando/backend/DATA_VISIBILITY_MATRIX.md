# Data Visibility Matrix — Chambeando Backend (Phase 2B)

Legend: ✅ full value · 🔒masked partial value · 👁 metadata only (never the sensitive value itself) · ❌ no access (endpoint 403/404s, or field never serialized).

Audiences, exactly as used in code (`services/authorization.py`, `deps.py`):

- **PUBLIC** — no `Authorization` header at all.
- **MEMBER** — valid JWT + an `ACTIVE` `MembershipDB` row, any role.
- **MATCHED_COUNTERPARTY** — MEMBER whose wallet is `order.seller_wallet` or `order.buyer_wallet` for that specific order (`is_order_party`).
- **DISPUTE_ARBITRATOR** — MEMBER whose wallet equals `order.arbiter_snapshot_wallet` **and** `order.onchain_status == DISPUTED` (`is_authorized_arbitrator`) — this is verified against the on-chain-mirrored field, never a backend-only role.
- **MODERATOR** / **ADMIN** — MEMBER with that backend role (`MembershipDB.role`), orthogonal to the on-chain arbiter.
- **SYSTEM** — the indexer process and the backend process itself (direct DB/adapter access, no HTTP boundary).

## Users / identity

| Field | PUBLIC | MEMBER | MATCHED_CP | ARBITRATOR | MODERATOR | ADMIN | SYSTEM |
|---|---|---|---|---|---|---|---|
| `alias` (display name) | ❌ | ✅ (via order listings, not a dedicated user-search endpoint — none exists) | ✅ | ✅ | ✅ | ✅ | ✅ |
| `wallet_address` (full) | ❌ | ❌ (never returned for anyone but self) | 🔒 masked in `OrderMemberOut`; the counterparty's *own* wallet is knowable to them independently (they control it), not served by us in full either | 🔒 masked, same as MEMBER | ❌ | ❌ (no `GET /users/{id}` exposing another wallet exists) | ✅ |
| `wallet_address` (own, via `GET /me`) | — | ✅ (it's their own) | — | — | — | — | — |
| `email` / `phone` / legal name | — | — | — | — | — | — | *(field does not exist in the schema at all in this phase — nothing to leak)* |
| `created_at` (account age) | ❌ | derived only, via `account_age_days` in reputation | same | same | ❌ | ❌ | ✅ |

## Membership

| Field | PUBLIC | MEMBER (self) | MEMBER (other) | MODERATOR | ADMIN | SYSTEM |
|---|---|---|---|---|---|---|
| own `role`/`status`/`joined_at` (`GET /me`) | ❌ | ✅ | — | — | — | ✅ |
| any member's `role`/`status`/`suspended_reason` (`MembershipAdminOut`, `GET /admin/memberships`) | ❌ | ❌ | ❌ | ❌ (no moderator-facing membership-list endpoint in this phase) | ✅ | ✅ |
| `invited_by_user_id` | ❌ | ❌ | ❌ | ❌ | ✅ (in `MembershipAdminOut`? — **not currently included**, see §14 gap below) | ✅ |

## Orders (marketplace) — `OrderMemberOut`

| Field | PUBLIC | MEMBER | MATCHED_CP | ARBITRATOR | MODERATOR | ADMIN |
|---|---|---|---|---|---|---|
| `onchain_status`, `crypto_amount`, `fiat_amount`, `fiat_currency`, `quoted_rate`, `created_at` | ❌ | ✅ | ✅ | ✅ | ✅ (same MEMBER view — no admin order-list endpoint exists, ADMIN uses MEMBER access like anyone) | ✅ (via DB) |
| `payment_method` (generic, e.g. "Transferencia CUP") | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `seller_wallet_masked` / `buyer_wallet_masked` | ❌ | 🔒 | 🔒 | 🔒 | 🔒 | ✅ (via DB) |
| `seller_wallet` / `buyer_wallet` (raw) | ❌ | ❌ | ❌ (even the counterparty gets the masked field from this endpoint — full wallets are simply not re-served since both parties already know their own; the *other* party's full wallet is not exposed by this endpoint at all in this phase) | ❌ | ❌ | ✅ (via DB / `MembershipAdminOut` does not include order wallets either) |
| `arbiter_snapshot_wallet`, `settlement_detail_id`, `was_disputed`, `onchain_order_id`, `escrow_tx_hash`, `token_address`, `created_by_user_id`, `confirmed_block` | ❌ | ❌ (not in `OrderMemberOut` — see `test_raw_orm_fields_cannot_leak_through_order_list`) | ❌ | ❌ | ❌ | ✅ (via DB only — no endpoint serializes these) |

## Settlement details (fiat payment instructions) — the most sensitive object

| Field | PUBLIC | MEMBER (unrelated) | MATCHED_CP (own trade) | ARBITRATOR (disputed trade only) | MODERATOR | ADMIN |
|---|---|---|---|---|---|---|
| `payment_method`, `currency`, `active`, `created_at`, `revoked_at` (`SettlementDetailOut`) | ❌ | ❌ (only the owner lists their own via `GET /settlement-details`) | ❌ (not exposed via this endpoint to the counterparty — they get the *revealed* view instead, see below) | ❌ | ❌ | ❌ (no admin listing of other users' settlement metadata exists) |
| decrypted `payload` (bank account, Zelle, etc.) via `GET /orders/{id}/settlement` | ❌ | ❌ | ✅ — **only** for the order they're party to, **only** while the linked detail is `active` | ✅ — **only** while `onchain_status == DISPUTED` and caller's wallet == that order's `arbiter_snapshot_wallet` | ❌ (moderators get evidence access while disputed, but **not** settlement — see `services/authorization.can_view_settlement_detail`, deliberately narrower than evidence) | ❌ (no ADMIN bypass exists — ADMIN has zero code path to decrypt any settlement detail) |
| `encrypted_payload` (ciphertext) | ❌ | ❌ | ❌ (never serialized raw, always decrypted-or-nothing) | ❌ | ❌ | ✅ only via direct DB access (SYSTEM), never via any HTTP response |

## Dispute evidence

| Field | PUBLIC | MEMBER (unrelated) | Order party (either side) | ARBITRATOR (disputed) | MODERATOR/ADMIN (disputed only) | MODERATOR/ADMIN (non-disputed) |
|---|---|---|---|---|---|---|
| metadata (`DisputeEvidenceOut`: type, `content_hash`, `note`, `created_at`) | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ |
| raw file content (`GET /disputes/evidence/{id}/content`) | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ |

`note` is free text submitted by the user themselves as their own evidence — it is not independently redacted (a user could, in theory, paste sensitive text into their own evidence note; that's a user-input risk noted in §13, not something the backend can distinguish from legitimate evidence content).

## Reputation (`ReputationOut`)

| Field | PUBLIC | MEMBER | MODERATOR | ADMIN |
|---|---|---|---|---|
| `completed_trades`, `cancelled_trades`, `disputes_opened/won/lost`, `account_age_days`, `successful_volume` | ❌ | ✅ (any member can view any other member's summary — these are aggregate trade stats, not independently sensitive) | ✅ | ✅ |

No endpoint anywhere accepts these as **input** — see `test_api_cannot_submit_its_own_completed_trade_count`.

## Security / audit events (`SecurityEventOut`)

| Field | PUBLIC | MEMBER | MODERATOR | ADMIN |
|---|---|---|---|---|
| `actor_user_id`, `action`, `target_type`, `target_id`, `reason`, `created_at` | ❌ | ❌ | ❌ (no moderator-facing audit endpoint in this phase) | ✅ (`GET /admin/security-events`) |
| the sensitive value itself (bank number, evidence content, etc.) | — | — | — | **never exists in this table at all** — structurally absent, not just access-controlled |

## Known gap in this matrix (tracked, not fixed in this phase)

`MembershipAdminOut` (ADMIN's membership list/detail view) does not currently include `invited_by_user_id`, even though the model has it — ADMIN would plausibly want it for abuse investigation ("who vouched for this suspended user"). Left out because adding it wasn't exercised by any required test and I'd rather under-expose by default than guess at an ADMIN need — flagged in §13 as non-blocking technical debt, trivial to add (one field in one schema) when actually needed.
