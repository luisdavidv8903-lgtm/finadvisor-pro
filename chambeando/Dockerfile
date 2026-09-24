# Chambeando backend -- production image (Docker-portable, provider-neutral).
#
# Deployment target is deliberately open: this image is meant to run
# unmodified on a plain VPS (docker run) or on any Docker-compatible platform
# (Fly/Railway/Render/ECS/k8s/...) -- nothing here assumes a specific host.
# See DEPLOYMENT_RUNBOOK.md for build/run/migrate/rollback steps.
#
# Build context is THIS directory (chambeando/), not the repo root:
#   docker build -t chambeando-backend -f Dockerfile .
# (run from inside chambeando/). Keeping the context scoped here -- instead
# of the whole finadvisor-pro monorepo -- keeps the build fast and never
# sends chambeando-v4/, chambeando-clean/, security-audits/, or contracts/
# node_modules/artifacts to the Docker daemon (see .dockerignore).

FROM python:3.12-slim AS base

# WORKDIR is the PARENT of the `backend` package, not backend/ itself:
# backend/main.py and backend/alembic/env.py both use package-relative
# imports (`from .config import settings`, `from backend.config import
# settings`) that require `backend` to be importable as a real package --
# i.e. this directory, containing `backend/`, must be on sys.path. This
# mirrors exactly how pytest.ini (pythonpath=.) and alembic/env.py's own
# sys.path.insert already run things -- verified by actually booting this
# exact invocation before shipping it, since the dev-docs' `--app-dir .`
# shortcut does NOT work here (relative imports fail without a real parent
# package).
WORKDIR /app

RUN groupadd --system chambeando && useradd --system --gid chambeando --no-create-home chambeando

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r backend/requirements.txt

# Application code -- everything the running process and `alembic upgrade
# head` need (see DEPLOYMENT_RUNBOOK.md section F). Explicitly NOT copied:
# .env/.env.*/*.secrets (blocked by .dockerignore, and never present in this
# repo checkout to begin with -- see the prior security audit of this
# branch), tests, local dev scripts, and every other repo directory outside
# chambeando/.
COPY backend/ ./backend/

# Never run the process as root inside the container.
RUN chown -R chambeando:chambeando /app
USER chambeando

# Bind address is fixed (0.0.0.0 -- required for the container's network
# namespace); the port is operator-configurable via $PORT so the same image
# works behind whatever port convention the host platform expects.
ENV PORT=8000
EXPOSE 8000

# Lightweight liveness probe against GET /health (see backend/main.py) --
# pure Python stdlib so the image never needs curl/wget installed just for
# this.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=2).read(); sys.exit(0)" || exit 1

# Single Uvicorn worker: correct default for a small instance (see
# DEPLOYMENT_RUNBOOK.md for scaling to multiple workers/instances -- that
# requires DATABASE_URL to point at a real, shared Postgres, never the
# SQLite fallback). No --reload in production. Logs go to stdout/stderr
# (Uvicorn's default), never to a file inside the image/container.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --no-server-header"]
