# WhatsApp Sandbox Interface — Phase 2C

WhatsApp is an interaction layer only. Authoritative state remains the
backend database + on-chain escrow contract, exactly as before this phase.
Nothing in `backend/messaging/` or `backend/routers/whatsapp.py` computes a
membership, order-state, settlement, or dispute decision itself — every such
decision is a direct call into the existing function that the HTTP API
already uses (`deps.get_current_membership`, `services.invites.redeem_invite_for_user`,
`services.authorization.*`, `routers.orders.attach_order_metadata`).

## 1. Architecture

```
WhatsApp webhook (POST /whatsapp/webhook)
    -> signature verification (HMAC-SHA256, X-Hub-Signature-256)
    -> idempotency check (ProcessedWebhookEventDB, DB-unique-constraint-enforced)
    -> rate limiting (per whatsapp_id, RateLimiter — same port as auth's)
    -> messaging.router.ConversationRouter.handle_inbound()
        -> messaging.identity (WhatsApp <-> User mapping)
        -> EXISTING Chambeando services (deps, services.invites, services.authorization,
           routers.orders.attach_order_metadata)
        -> P2POrderDB (read-only from this layer)
    -> messaging.adapter.MessagingAdapter.send() (outbound, safe-templated only)
```

`MessagingAdapter` is a channel-agnostic port (`messaging/adapter.py`).
`WhatsAppAdapter` (`messaging/whatsapp_adapter.py`) is the only implementation
in this phase, backed by `SandboxMetaClient` — an in-memory fake that never
makes a network call, the same role `FakeChainAdapter` plays for the chain.
Adding Telegram/web-chat later means a new `MessagingAdapter` implementation,
never a rewrite of `ConversationRouter`.

## 2. Identity mapping — three separate facts, never collapsed

| Fact | Table/column | Proven by |
|---|---|---|
| "This WhatsApp id has talked to the bot" | `WhatsAppLinkDB.whatsapp_id` exists | Nothing — this is the weakest possible claim |
| "This WhatsApp id proved wallet ownership" | `WhatsAppLinkDB.user_id` is set | A real nonce/signature flow (`/auth/nonce` + `/auth/verify`, unchanged) followed by `POST /whatsapp/link` with the resulting JWT |
| "That wallet is a marketplace member" | A `MembershipDB` row exists, `status=ACTIVE` | `services.invites.redeem_invite_for_user` (unchanged) |

**WhatsApp account ≠ wallet identity ≠ membership.** Possessing a WhatsApp
number never implies wallet ownership; wallet ownership never implies
membership. `ConversationRouter._require_linked_member` enforces this by
calling the real `get_current_membership` dependency directly (not a
WhatsApp-specific reimplementation) and fails closed at every step.

## 3. Wallet / signature handoff (section 4)

WhatsApp never asks for or transmits a private key or seed phrase — grep
`backend/messaging/` and `backend/routers/whatsapp.py`; neither string
appears anywhere except in comments explicitly forbidding it.

Flow: `JOIN` → `messaging.identity.issue_link_token` creates a short-lived
(10 min default), single-use, **hashed** (never plaintext-persisted) token →
a deep link (`{dapp_base_url}/link?token=...`) is sent → the (mocked, this
phase) dApp page runs the **existing** `/auth/nonce` + `/auth/verify` wallet-
signature flow → calls `POST /whatsapp/link` with the resulting JWT →
`get_current_user` (the same dependency every other authenticated route
uses) proves the caller really is that wallet → `consume_link_token` binds
`whatsapp_id -> user_id`, single-use, and rejects tokens already bound to a
different wallet.

The same handoff pattern (deep link → mocked dApp → real backend route) is
used for: claiming an offer (`/claim?order_id=`), publishing a sell offer
(`/create-order?...`), and disputing/submitting evidence
(`/dispute?order_id=`) — none of these are backend HTTP endpoints new to
this phase; WhatsApp only hands off to them, since the actual actions
(claim/create-order/raise-dispute) are on-chain wallet signatures the
backend cannot perform on the user's behalf.

## 4. Webhook security (section 6)

- **Verification handshake**: `GET /whatsapp/webhook` checks
  `hub_mode == "subscribe"` and `hub_verify_token == settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN`
  (constant-time compare) before echoing `hub_challenge` back.
- **Payload authenticity**: `POST /whatsapp/webhook` computes
  `HMAC-SHA256(app_secret, raw_body)` and compares it (constant-time) against
  `X-Hub-Signature-256` — the actual scheme Meta's Cloud API uses. A missing,
  wrong, or body-mismatched signature is a 403, and the message is **never**
  dispatched to the conversation router.
- **Idempotent inbound processing / duplicate delivery**: `ProcessedWebhookEventDB`
  has a DB-level `UNIQUE(provider, message_id)` constraint — the actual
  guarantee, not just a Python `if exists` check (same pattern as `InviteDB.code_hash`).
  A second delivery of the same `message_id` hits `IntegrityError`, is rolled
  back, counted as a duplicate, and never reaches `ConversationRouter`.
- **Rate limiting**: reuses `security/rate_limit.py`'s existing `RateLimiter`
  port, keyed per `whatsapp_id`.
- **Safe logging**: `routers/whatsapp.py` never logs a message body/text —
  only counts and metadata, the same "log WHO/WHAT/WHEN, never the sensitive
  VALUE" discipline `security/audit.py` already established.

## 5. Privacy (section 3)

- Every outbound template in `messaging/notifications.py` is a closed set of
  pre-written strings with only non-sensitive interpolation (an order
  reference number, a status word) — never an f-string built from a
  settlement payload, evidence note, or dispute reason.
- Settlement reveal and evidence submission/viewing are **never** done by
  typing into WhatsApp — the conversation router only ever hands off a
  secure deep link to the existing, already-privacy-audited
  `/orders/{id}/settlement` and `/disputes/*` routes.
- `BUY`/`MY TRADES` listings use `services.authorization.mask_wallet` (reused,
  not reimplemented) — full wallet addresses never appear in a WhatsApp
  message.
- `DisputeAssignment` remains the only path for staff (MODERATOR/ADMIN) to
  see evidence of a dispute they are not a party to — a WhatsApp session
  authenticated as staff gets **no** special-cased access; `can_view_dispute_evidence`
  is called exactly as the HTTP route calls it.

## 6. Conversation state (section 7)

`ConversationSessionDB` is the sole source of truth for "what is this
WhatsApp user in the middle of doing" — never WhatsApp's own message
history. Order/dispute state is never duplicated into this table, only
referenced by database id inside a session's small JSON `context` (e.g.
which displayed offer number maps to which real order id, for *this*
session only). Money/order-state itself is always read fresh from
`P2POrderDB` at the moment of use, so a trade that changes state outside
WhatsApp (via the real dApp/contract) is reflected the next time the user
interacts, without any special reconciliation code — "backend truth" is the
only truth this layer ever reads.

## 7. Remaining blockers before a REAL Meta WhatsApp sandbox

1. **Real Meta Cloud API client.** `SandboxMetaClient` never calls the
   network. A production `MetaClient` implementation (auth, retries, media
   upload for future rich messages) is unwritten.
2. **Real webhook envelope.** `WhatsAppWebhookPayload`/`WhatsAppInboundMessage`
   (`schemas.py`) are a deliberately simplified synthetic shape, not Meta's
   actual deeply-nested `entry[].changes[].value.messages[]` JSON. Parsing
   the real envelope (and its message-type variants: text, interactive
   button replies, media) is unwritten.
3. **`hub.mode`/`hub.verify_token`/`hub.challenge` query param names.** Meta
   sends dotted names FastAPI cannot bind directly to Python identifiers;
   this phase's `GET /whatsapp/webhook` uses the underscore spelling
   (`hub_mode`), which needs an edge/proxy rewrite (or a raw-`Request`
   handler) in front of a real deployment.
4. **Real phone number / WABA provisioning**, Meta Business verification,
   and template-message pre-approval (Meta requires pre-approved templates
   for many outbound message types outside a 24-hour customer-service
   window) — none of this is a code problem, all of it is an operational
   Meta-side setup step outside this phase's authorized scope.
5. **Production rate limiting / conversation-session storage** — both
   currently reuse the same in-memory, single-process-only implementations
   already flagged as dev/test-only in `ENFORCEMENT_LEVELS.md`; a real
   deployment needs Redis (or equivalent) for both, same as documented there.
6. **Real deep-link resolution.** The `dapp_base_url` used throughout this
   phase (`https://chambeando.local/app/...`) is a placeholder; no actual
   web/dApp frontend exists yet to receive these links and drive the wallet
   signature UI.
