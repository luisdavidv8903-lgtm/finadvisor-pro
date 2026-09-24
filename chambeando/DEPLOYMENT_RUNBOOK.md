# Chambeando backend — Deployment Runbook

Provider-neutral: everything below assumes a plain Docker host (a VPS running
`dockerd`, or any Docker-compatible platform — Fly/Railway/Render/ECS/k8s/...).
Nothing here is specific to one hosting provider; picking a provider and
writing its provider-specific config (`render.yaml`, `fly.toml`, etc.) is a
deliberately separate, later step.

No credentials, tokens, or real WABA/App/bot identifiers appear anywhere in
this document. Every value below is a variable NAME, never a value.

## A. Build

Run from inside `chambeando/` (the build context — see `Dockerfile`'s own
comment for why it's scoped here and not the repo root):

```bash
cd chambeando
docker build -t chambeando-backend:latest -f Dockerfile .
```

**Note on invocation style**: this image runs the app as `backend.main:app`
from `/app` (the parent of the `backend` package), NOT `main:app --app-dir
.` from inside `backend/` as `chambeando/README.md`'s local-dev section
shows. That was verified by actually booting both: `main:app --app-dir .`
fails with `ImportError: attempted relative import with no known parent
package`, because `main.py`'s `from .config import settings` needs `backend`
importable as a real package, not a bare top-level module. `backend.main:app`
run from one level up — the same layout `pytest.ini` (`pythonpath = .`) and
`backend/alembic/env.py` already use — works correctly. This is a pre-existing
gap in the README's dev instructions, not something introduced here; flagging
it rather than silently rewriting README.md, which is outside this
iteration's scope.

## B. Run

```bash
docker run -d \
  --name chambeando-backend \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file /path/to/production.env \
  chambeando-backend:latest
```

- `--restart unless-stopped` gives you auto-restart on crash/host reboot
  without any orchestrator. A platform with its own process supervisor
  (systemd unit calling `docker run`, or a Docker-compatible PaaS) may
  replace this with its own restart policy instead.
- `--env-file` — see section C. `production.env` itself must never be
  committed to git, baked into the image, or added to the Docker build
  context (already excluded by `.dockerignore`).
- The container's internal port is fixed at `8000` unless you override `PORT`
  in the env file, in which case change the `-p` mapping's container-side
  port (`-p 8000:$PORT`) to match.

## C. Environment variables

Names are exactly what `backend/config.py` reads (`Settings`) — no invented
duplicates. See `backend/.env.example` for the same list with placeholder
values and inline comments.

### REQUIRED

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string. **Must** be set explicitly in any real deployment — see section F for why the built-in default is dangerous. |
| `SECRET_KEY` | Signs session JWTs. App refuses to start without it (`Settings` has no default). |
| `SETTLEMENT_ENCRYPTION_KEY` | Fernet key encrypting settlement details at rest. App refuses to start without it. |

### OPTIONAL (safe defaults exist; override per-environment)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | Port Uvicorn binds inside the container (see Dockerfile `CMD`). |
| `ALGORITHM` | `HS256` | JWT signing algorithm. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | Session JWT lifetime. |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | Must be set to the real frontend origin(s) in production. |
| `CHAIN_ADAPTER` | `tron` | Only `"tron"` is implemented. |
| `TRON_NODE_URL` | Shasta testnet | **Change to a mainnet node before any real deployment** — testnet by default on purpose. |
| `ESCROW_CONTRACT_ADDRESS` | empty | Required once the contract is actually deployed (out of scope for this runbook — see `chambeando/README.md`). |
| `CONFIRMATIONS_REQUIRED` | `19` | Indexer confirmation depth. |
| `WHATSAPP_PROVIDER` | `sandbox` | Set to `meta` only once `WHATSAPP_ACCESS_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID` are real. |
| `WHATSAPP_ACCESS_TOKEN` | empty | Meta Cloud API access token — REQUIRED once `WHATSAPP_PROVIDER=meta`. |
| `WHATSAPP_PHONE_NUMBER_ID` | empty | Meta phone number id — REQUIRED once `WHATSAPP_PROVIDER=meta`. |
| `WHATSAPP_VERIFY_TOKEN` | public placeholder | Token Meta echoes during the GET webhook handshake — set to a real random value before registering the webhook (see section F). |
| `WHATSAPP_APP_SECRET` | public placeholder | HMAC key for `X-Hub-Signature-256`. **Must** be overridden — see section D, this is the crux of the fail-closed behavior added in this iteration. |
| `WHATSAPP_ALLOW_UNVERIFIED_WEBHOOKS` | `false` | Keep `false` in production. See section D. |
| `WHATSAPP_GRAPH_API_VERSION` | `v21.0` | Meta Graph API version pin. |
| `RATE_LIMIT_WHATSAPP_MESSAGE_PER_MINUTE` | `20` | Per-`whatsapp_id` inbound rate limit. In-memory limiter — see note below. |
| `TELEGRAM_PROVIDER` | `sandbox` | Set to `telegram` only once `TELEGRAM_BOT_TOKEN` is real. |
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token — REQUIRED once `TELEGRAM_PROVIDER=telegram`. |
| `TELEGRAM_WEBHOOK_SECRET` | public placeholder | Value Telegram echoes back as `X-Telegram-Bot-Api-Secret-Token` — set to a real random value before registering the webhook. |
| `RATE_LIMIT_TELEGRAM_MESSAGE_PER_MINUTE` | `20` | Per-`channel_user_id` inbound rate limit. |
| `RATE_LIMIT_NONCE_PER_MINUTE` / `RATE_LIMIT_VERIFY_PER_MINUTE` / `RATE_LIMIT_INVITE_REDEEM_PER_MINUTE` | `5` / `10` / `5` | Auth-flow rate limits. |
| `WHATSAPP_LINK_TOKEN_EXPIRE_SECONDS` | `600` | Wallet-link token TTL. |
| `NONCE_EXPIRE_SECONDS` | `300` | Auth nonce TTL. |
| `INDEXER_POLL_INTERVAL_SECONDS` / `INDEXER_MAX_BLOCK_RANGE` | `5` / `500` | Only relevant if you also run the separate indexer process (`python -m indexer`) — not part of this image's `CMD`. |

**Rate limiter note**: `security/rate_limit.py`'s limiter is in-memory —
per-process, reset on restart, not shared across replicas. Fine for a single
instance; if you scale to multiple replicas behind a load balancer, replace
it with Redis first (out of scope here — flagged, not implemented, per this
iteration's "don't change DB/architecture unnecessarily" instruction).

### DEVELOPMENT_ONLY (never set these in production)

| Variable | Why dev-only |
|---|---|
| `WHATSAPP_ALLOW_UNVERIFIED_WEBHOOKS=true` | Deliberately lifts the fail-closed refusal on a still-default `WHATSAPP_APP_SECRET` — only for local testing of the webhook route without a real Meta App Secret. |
| `WHATSAPP_PROVIDER=sandbox` / `TELEGRAM_PROVIDER=sandbox` | Fine as dev defaults; a real deployment that's supposed to actually send messages needs the real provider. |
| `TRON_NODE_URL` left at the Shasta testnet default | Never point production settlement logic at testnet. |
| `DATABASE_URL` left unset (SQLite fallback) | See section F — actively dangerous outside local dev. |

## D. WhatsApp signature fail-closed behavior (this iteration's hardening)

`WHATSAPP_APP_SECRET`'s default (`"sandbox-app-secret-never-use-in-production"`)
is a **public string committed to this repository**. Before this iteration,
leaving it unconfigured in a real deployment meant `verify_webhook_signature`
would still "succeed" for anyone who read `config.py` and signed a forged
request with that same known value — the endpoint would appear
signature-protected while actually being open to spoofed inbound events.

`POST /whatsapp/webhook` now checks `webhook_signature_check_is_trustworthy()`
(`backend/messaging/whatsapp_adapter.py`) before looking at any signature: if
`WHATSAPP_APP_SECRET` is still that public default, **every POST is rejected
with 503**, regardless of what `X-Hub-Signature-256` is presented — unless
`WHATSAPP_ALLOW_UNVERIFIED_WEBHOOKS=true` was deliberately set. That flag
defaults to `false` everywhere and is never inferred from the secret being
empty/default. **In production, the fix is always to set a real
`WHATSAPP_APP_SECRET` — never to set this flag.**

## E. Health check

`GET /health` → `200 {"status": "ok", "app": "chambeando-backend"}`. No auth,
no DB query, no external calls — safe for a load balancer or the Docker
`HEALTHCHECK` (already wired into the image) to poll frequently.

## F. Database & migration procedure

**Startup does NOT run migrations automatically.** `backend/main.py` documents
this explicitly: schema is Alembic-managed, not `create_all()` — `alembic
upgrade head` is a separate, deliberate step, run before the API process
starts (or as part of a release step your platform runs pre-deploy).

```bash
docker run --rm \
  --env-file /path/to/production.env \
  --entrypoint alembic \
  chambeando-backend:latest -c backend/alembic.ini upgrade head
```

(Or `docker exec` into a running container and run the same `alembic -c
backend/alembic.ini upgrade head` from `/app` -- `backend/alembic/env.py`
inserts `/app` onto `sys.path` itself, so this works from the image's
`WORKDIR` regardless of shell cwd quirks.)

**Material risk found and documented, not fixed in this iteration** (per
instruction: don't change DB architecture unnecessarily) — `DATABASE_URL`
defaults to `sqlite:///./chambeando_v2.db` (`config.py`) if unset. In any
container-based hosting with an ephemeral filesystem (which is the norm — a
plain `docker run` with no mounted volume, and effectively every
Docker-compatible PaaS), this means:

1. **Every restart/redeploy silently wipes all data** — no crash, no
   warning, just a fresh empty SQLite file.
2. **Multiple replicas would each get their own separate SQLite file**,
   silently diverging instead of sharing state.

**Proposed fix (not applied — flagging for your decision, not a silent
architecture change):** add a startup guard, mirroring the pattern just added
for `WHATSAPP_ALLOW_UNVERIFIED_WEBHOOKS`, that refuses to start when
`DATABASE_URL` is unset/still the SQLite default AND an explicit "production
mode" flag is set — so a misconfigured deploy fails loudly instead of quietly
losing data on the next restart. Until that exists: **operationally**,
always set `DATABASE_URL` to a real Postgres connection string in any
deployment that must survive a restart, and treat "SQLite in production" as
a deploy-checklist blocker (see section N).

## G. WhatsApp webhook configuration (Meta side)

1. Set real `WHATSAPP_VERIFY_TOKEN` and `WHATSAPP_APP_SECRET` in the
   deployment's env (never the placeholders).
2. In the Meta App dashboard, register the webhook URL as
   `https://<your-domain>/whatsapp/webhook`, using the same
   `WHATSAPP_VERIFY_TOKEN` value for the verify-token field. Meta performs a
   `GET` handshake (`hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`)
   which `routers/whatsapp.py`'s `verify_subscription` answers.
3. Once verified, Meta sends real events to the same URL as signed `POST`s
   (`X-Hub-Signature-256`), which the fail-closed check in section D now
   guards.
4. To actually send outbound messages (not just receive), set
   `WHATSAPP_PROVIDER=meta` plus `WHATSAPP_ACCESS_TOKEN` and
   `WHATSAPP_PHONE_NUMBER_ID` — otherwise the app stays on
   `SandboxMetaClient` (records sends in-memory, never calls the network).

## H. Telegram webhook configuration

1. Set real `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`.
2. Register the webhook via Telegram's Bot API (`setWebhook`), pointing at
   `https://<your-domain>/telegram/webhook` and passing
   `secret_token=<TELEGRAM_WEBHOOK_SECRET>` — Telegram echoes it back as the
   `X-Telegram-Bot-Api-Secret-Token` header on every delivery, which
   `routers/telegram.py` verifies.
3. Set `TELEGRAM_PROVIDER=telegram` to switch off the sandbox client for
   outbound sends.

## I. Secret rotation

1. Generate the new value out-of-band (never derive it from the old one).
2. Update it in the deployment's env store (whatever mechanism your platform
   uses — never commit it, never put it in the Docker image/build context).
3. Restart the container so the new process picks it up (`Settings` is
   read once at import time — no hot reload of env vars).
4. For `WHATSAPP_APP_SECRET`/`WHATSAPP_VERIFY_TOKEN`/`TELEGRAM_WEBHOOK_SECRET`
   specifically: update the corresponding value on the Meta/Telegram side
   (App dashboard / `setWebhook`) in the same maintenance window — a
   mismatch between what the provider signs with and what this app expects
   means every webhook gets rejected (fail-closed, so this fails loud, not
   silent).
5. Old value stops working the moment step 3 completes — no overlap/grace
   period is implemented, so rotate provider-side and app-side together.

## J. Logs

Uvicorn logs to stdout/stderr by default (unchanged by this iteration) — the
container never writes a log file. Capture them with your platform's normal
mechanism (`docker logs chambeando-backend`, or whatever log aggregation the
host provides). The codebase's existing "safe logging" rule (see
`WHATSAPP_ARCHITECTURE.md`, `security/audit.py`) already keeps message
bodies/secrets out of every log line this app emits — nothing about
containerizing it changes that guarantee.

## K. Restart

```bash
docker restart chambeando-backend
```

No special drain/quiesce procedure exists today (no in-flight-request
tracking beyond what Uvicorn/the OS already do). A webhook POST that's
mid-flight during a restart gets whatever TCP-level behavior Docker/the host
gives it; Meta/Telegram both retry failed deliveries, and this app's
DB-enforced idempotency (`ProcessedWebhookEventDB` unique constraint) makes a
redelivered event safe to reprocess.

## L. Rollback

```bash
docker run -d --name chambeando-backend --restart unless-stopped \
  -p 8000:8000 --env-file /path/to/production.env \
  chambeando-backend:<previous-tag>
```

Tag every build you deploy (`docker build -t chambeando-backend:<git-sha>`)
so a previous image is always addressable. **Database rollback is a separate
concern**: `alembic downgrade` exists and is exercised by
`test_migrations.py`, but running it against real production data is a
deliberate, manual decision outside this runbook's scope — never automate it
as part of a container rollback.

## M. What must NEVER be stored in the image

- `.env` / `.env.*` / `*.secrets` (blocked by `.dockerignore`; also never
  present in this repo checkout — confirmed in the prior security audit of
  this branch).
- Any real `WHATSAPP_APP_SECRET`, `WHATSAPP_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`,
  `SECRET_KEY`, `SETTLEMENT_ENCRYPTION_KEY`, or `DATABASE_URL` with embedded
  credentials — all of these come from the container's environment at
  runtime (`--env-file`), never from a `COPY`'d file or an `ENV` in the
  Dockerfile.
- `*.db` / `db_backups/` / `*.log` (blocked by `.dockerignore`) — this image
  is stateless; the SQLite-fallback risk in section F is exactly the
  scenario where state would otherwise leak into a container's writable
  layer.
- `backend/tests/` — not needed at runtime, excluded from the build context.

## N. Checklist before production

- [ ] `DATABASE_URL` set to a real Postgres instance (never the SQLite
      default — section F).
- [ ] `alembic upgrade head` run against that database (section F).
- [ ] `SECRET_KEY` and `SETTLEMENT_ENCRYPTION_KEY` set to real, random,
      never-committed values.
- [ ] `CORS_ORIGINS` set to the real frontend origin(s), not `localhost`.
- [ ] `TRON_NODE_URL` pointed at mainnet, `ESCROW_CONTRACT_ADDRESS` set,
      contract audited (see `chambeando/README.md` — explicitly NOT audited
      as of this iteration).
- [ ] `WHATSAPP_APP_SECRET` and `WHATSAPP_VERIFY_TOKEN` set to real values;
      `WHATSAPP_ALLOW_UNVERIFIED_WEBHOOKS` left `false` (section D).
- [ ] `WHATSAPP_PROVIDER=meta` with real `WHATSAPP_ACCESS_TOKEN` /
      `WHATSAPP_PHONE_NUMBER_ID`, only once the WABA is actually
      production-ready (see this branch's earlier audit re: the separate
      production-WABA `error 141008` issue — unrelated to this checklist).
- [ ] `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET` set; `TELEGRAM_PROVIDER=telegram`.
- [ ] `GET /health` returns 200 through the platform's load balancer/health
      check path, not just inside the container.
- [ ] Image built and tagged with a traceable identifier (git SHA) — section L.
- [ ] Full non-Postgres test suite green, plus the Postgres-dependent suite
      run at least once against the real target Postgres version before
      first deploy (`test_postgres_validation.py`, `test_e2e_postgres.py`,
      `test_e2e_whatsapp_postgres.py` — not exercised by this runbook's CI
      path, see the audit report for why).
- [ ] Legal/regulatory blocker from `chambeando/README.md` (VASP/CACR/OFAC —
      which entity legally operates this) resolved. Explicitly out of scope
      for this runbook, but blocking for any real launch regardless of how
      solid the hosting is.
