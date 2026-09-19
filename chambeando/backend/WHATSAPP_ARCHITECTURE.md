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

- **Verification handshake**: `GET /whatsapp/webhook` binds Meta's REAL
  dotted query params (`hub.mode`, `hub.verify_token`, `hub.challenge`) via
  FastAPI `Query(alias=...)` — no proxy/edge rewrite needed (Phase 2D.1 fixed
  the Phase 2C limitation here). Checks `hub.mode == "subscribe"` and
  `hub.verify_token == settings.WHATSAPP_VERIFY_TOKEN` (constant-time
  compare) before echoing `hub.challenge` back; the verify token itself is
  never echoed in any response.
- **Payload authenticity**: `POST /whatsapp/webhook` computes
  `HMAC-SHA256(app_secret, raw_body)` against the EXACT raw bytes received
  (`await request.body()`, read before any JSON parsing) and compares it
  (constant-time) against `X-Hub-Signature-256` — the actual scheme Meta's
  Cloud API uses. A missing, wrong, or body-mismatched signature is a 403,
  and the message is **never** dispatched to the conversation router.
- **Real envelope parsing** (Phase 2D.1, `messaging/meta_envelope.py`):
  the actual Meta `entry[].changes[].value.{messages[],statuses[],contacts[],metadata}`
  structure, not Phase 2C's simplified synthetic shape. `statuses` (delivery/
  read receipts for messages WE sent) are structurally separate from
  `messages` and are never converted into inbound conversational input.
  Unsupported message types (image, audio, location, etc.) are safely
  skipped, not crashed on; unknown/future Meta fields are ignored
  (`extra="ignore"` throughout) rather than rejected.
- **Idempotent inbound processing / duplicate delivery**: `ProcessedWebhookEventDB`
  has a DB-level `UNIQUE(provider, message_id)` constraint — the actual
  guarantee, not just a Python `if exists` check (same pattern as `InviteDB.code_hash`).
  Meta's own message id (`wamid...`) is the external idempotency key. A
  second delivery of the same `message_id` hits `IntegrityError`, is rolled
  back, counted as a duplicate, and never reaches `ConversationRouter`.
- **Rate limiting**: reuses `security/rate_limit.py`'s existing `RateLimiter`
  port, keyed per `whatsapp_id`.
- **Safe logging**: `routers/whatsapp.py` never logs a message body/text —
  only counts and metadata, the same "log WHO/WHAT/WHEN, never the sensitive
  VALUE" discipline `security/audit.py` already established.

## 4a. Real Meta Cloud API outbound client (Phase 2D.1, section 2/7)

`messaging/whatsapp_adapter.py`'s `MetaCloudWhatsAppClient` implements the
`MetaClient` port for real Meta Graph API sends (`POST /{graph_api_version}/{phone_number_id}/messages`),
behind the exact same interface `SandboxMetaClient` implements. Provider
selection is **config-driven only** (`settings.WHATSAPP_PROVIDER`, `"sandbox"`
by default) — never inferred from whether credentials happen to be set, so
exporting `WHATSAPP_ACCESS_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID` locally for
testing can never silently start sending real messages.

- Configuration comes only from `settings.WHATSAPP_*` (environment
  variables) — never a hardcoded credential.
- The access token is used only as an `Authorization: Bearer <token>` header
  on outbound requests; it is never persisted to the database and never
  logged.
- On a non-2xx Meta response, only the HTTP status code and Meta's own
  numeric `error.code` are logged — never the raw response body (which could
  echo request content) and never any request header.
- No automatic retry: a WhatsApp send is not idempotent from Meta's side, so
  retrying a possibly-already-successful request risks a duplicate message
  to a real person — a single failed attempt raises `MetaApiError` and stops.

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

Resolved in Phase 2D.1 (kept here, struck through, so this list stays an
accurate history rather than silently dropping context):

1. ~~Real Meta Cloud API client.~~ **DONE** — `MetaCloudWhatsAppClient`
   (`messaging/whatsapp_adapter.py`), selected via `settings.WHATSAPP_PROVIDER`.
   Media upload for future rich messages remains unwritten (not needed for
   the text/button flows this product uses today).
2. ~~Real webhook envelope.~~ **DONE** — `messaging/meta_envelope.py` parses
   the real `entry[].changes[].value.{messages[],statuses[],contacts[],metadata}`
   structure (text + interactive button + quick-reply button; other types
   safely skipped).
3. ~~`hub.mode`/`hub.verify_token`/`hub.challenge` query param names.~~
   **DONE** — bound via FastAPI `Query(alias="hub.mode")` etc., no edge/proxy
   rewrite needed.

Still open:

4. **Real phone number / WABA provisioning**, Meta Business verification,
   and template-message pre-approval (Meta requires pre-approved templates
   for many outbound message types outside a 24-hour customer-service
   window) — none of this is a code problem, all of it is an operational
   Meta-side setup step outside this phase's authorized scope. See the
   Phase 2D.1 report for the exact human steps required.
5. **Production rate limiting / conversation-session storage** — both
   currently reuse the same in-memory, single-process-only implementations
   already flagged as dev/test-only in `ENFORCEMENT_LEVELS.md`; a real
   deployment needs Redis (or equivalent) for both, same as documented there.
6. **Real deep-link resolution.** The `dapp_base_url` used throughout this
   phase (`https://chambeando.local/app/...`) is a placeholder; no actual
   web/dApp frontend exists yet to receive these links and drive the wallet
   signature UI. Phase 2D.1 section 11 explicitly keeps this disabled —
   signature-required actions reply with a safe "wallet handoff not yet
   enabled" message instead of a real link once `WHATSAPP_PROVIDER=meta`.
7. **A publicly reachable HTTPS callback** for Meta to deliver webhooks to.
   `cloudflared` is already installed locally (see Phase 2D.1 report §11);
   no tunnel has been started in this phase (not authorized without a
   separate approval step).
8. **A real HTTP client for `MetaCloudWhatsAppClient`'s send path has never
   been exercised against the real Graph API** — only against
   `httpx.MockTransport` in tests. The first real call happens only after
   explicit authorization for a live test message (see Phase 2D.1 report
   §13/§21).
