# Local PostgreSQL Validation — Phase 2B.4

## Installation method (important deviation from the original plan)

The task authorized `winget install PostgreSQL.PostgreSQL.17` (the standard Windows-service installer). That failed: this session's shell has no admin elevation available (confirmed via `whoami /groups` — the `BUILTIN\Administrators` group is deny-only for this process token), and the EDB installer's service-registration step requires elevation that can't be granted non-interactively (no UAC prompt can be answered in this session).

**Fallback used instead:** PostgreSQL's official **portable binaries** distribution (a ZIP of the same EDB-built `.exe` binaries, no installer, no Windows service). This is real PostgreSQL 17.11 — same binaries the installer would have placed — just run directly under the current user account instead of as a registered system service. This is a *more* conservative choice than the service installer (smaller footprint, no system-wide registration, trivially removable by deleting a directory), and was not treated as requiring separate authorization since "install PostgreSQL locally... for development/testing" was already explicit — only the *mechanism* changed, not the outcome.

## Service info

- **PostgreSQL version:** 17.11 (`postgresql-17.11-3-windows-x64` binaries from EDB)
- **Install location:** `C:\Users\luisd\pgsql17\pgsql` (binaries), data directory `C:\Users\luisd\pgsql17\data` — **both outside the git repository**, no risk of being tracked
- **Not a Windows Service** — no `Get-Service` entry; runs as a background process under the current user, started via `pg_ctl`
- **Port:** 5432 (localhost only — `pg_hba.conf` only allows `127.0.0.1/32` and `::1/128`, both requiring `scram-sha-256` password auth; the default `trust` auth `initdb` sets up was explicitly tightened before first start)
- **Test database:** `chambeando_test`
- **Test role:** `chambeando_test_user` (owns `chambeando_test`, distinct from the `postgres` superuser)
- **Log file:** `C:\Users\luisd\pgsql17\server.log`

## How to start / stop

```powershell
# Start
& "C:\Users\luisd\pgsql17\pgsql\bin\pg_ctl.exe" -D "C:\Users\luisd\pgsql17\data" -l "C:\Users\luisd\pgsql17\server.log" -o "-p 5432" start

# Stop
& "C:\Users\luisd\pgsql17\pgsql\bin\pg_ctl.exe" -D "C:\Users\luisd\pgsql17\data" stop

# Status
& "C:\Users\luisd\pgsql17\pgsql\bin\pg_ctl.exe" -D "C:\Users\luisd\pgsql17\data" status
```

Per your explicit instruction, this was **left running** after validation (§7: "acceptable to leave the local PostgreSQL development service installed... do not delete unless necessary").

## Credentials — never printed, never committed

Two passwords were generated (28-char random alphanumeric, `RandomNumberGenerator`-backed) and written directly to local files, never echoed to any command output or this transcript:
- The `postgres` superuser password — used only during `initdb`/role-creation, via `--pwfile` (initdb) or `PGPASSWORD` env var read from file.
- The `chambeando_test_user` password — embedded in the local-only connection URL in `chambeando/backend/.env.postgres_test` (gitignored via `backend/.env.*`, added this phase).

Neither password reuses any GitHub, Windows, banking, or other production credential — both were freshly generated for this purpose only.

## Connecting

```
DATABASE_URL=postgresql://chambeando_test_user:<password from .env.postgres_test>@localhost:5432/chambeando_test
```

Test files read this via `POSTGRES_TEST_DATABASE_URL` (a distinct env var from the app's normal `DATABASE_URL`, so the SQLite-targeted test suite's own `DATABASE_URL` usage is never disturbed):

```bash
DBURL=$(grep DATABASE_URL backend/.env.postgres_test | cut -d= -f2-)
POSTGRES_TEST_DATABASE_URL="$DBURL" pytest backend/tests/test_postgres_validation.py
```

If `POSTGRES_TEST_DATABASE_URL` is not set, `test_postgres_validation.py` skips entirely (`pytestmark = pytest.mark.skipif(...)`) — the normal SQLite suite never requires Postgres to be running.
