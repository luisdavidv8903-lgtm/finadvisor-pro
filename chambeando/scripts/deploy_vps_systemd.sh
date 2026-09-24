#!/usr/bin/env bash
#
# deploy_vps_systemd.sh -- reproducible deploy of Chambeando (Option B:
# systemd + venv) onto the existing DELIVERYLINK VPS, where Barberbot
# (port 3001) is production and must never be touched.
#
# Converts the already-approved manual process (see
# chambeando/SERVER_DEPLOYMENT_RUNBOOK.md sections 2-3) into a reproducible
# script. Intended to run ON THE VPS, as root (systemd/`/opt`/`/etc` writes
# require it) -- NOT from this repo checkout's CI or from Claude Code Cloud,
# which has no access to that server.
#
# Usage:
#   deploy_vps_systemd.sh RELEASE_SHA [SOURCE_REPO_DIR]
#
#   RELEASE_SHA       Git commit SHA (7-40 hex chars) to deploy. Required.
#   SOURCE_REPO_DIR   Local git checkout of luisdavidv8903-lgtm/finadvisor-pro
#                     that already has RELEASE_SHA fetched. Defaults to
#                     /opt/chambeando/repo. This script never clones or
#                     fetches from a remote itself -- keeping the repo
#                     checkout up to date is a separate, deliberate step
#                     (so no remote credentials need to live in this script).
#
# What this script explicitly does NOT do:
#   - touch Caddy config, reload Caddy, or touch DNS
#   - touch Meta or Telegram configuration
#   - touch Barberbot (port 3001) in any way
#   - generate, print, or overwrite /etc/chambeando/chambeando.env
#   - bind to anything other than 127.0.0.1:8100

set -euo pipefail

# ---------------------------------------------------------------------------
# Fixed paths (match SERVER_DEPLOYMENT_RUNBOOK.md section 2 exactly)
# ---------------------------------------------------------------------------
BASE_DIR="/opt/chambeando"
RELEASES_DIR="${BASE_DIR}/releases"
CURRENT_LINK="${BASE_DIR}/current"
VENV_DIR="${BASE_DIR}/venv"
DATA_DIR="/var/lib/chambeando"
ENV_FILE="/etc/chambeando/chambeando.env"
SERVICE_NAME="chambeando.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"
CHAMBEANDO_PORT="8100"
BARBERBOT_PORT="3001"
HEALTH_URL="http://127.0.0.1:${CHAMBEANDO_PORT}/health"
DEFAULT_SOURCE_REPO_DIR="${BASE_DIR}/repo"

log() { printf '[deploy] %s\n' "$1"; }
fail() { printf '[deploy] FAILED: %s\n' "$1" >&2; exit 1; }

trap 'printf "[deploy] FAILED at line %s -- no secret values were printed above.\n" "$LINENO" >&2' ERR

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
RELEASE_SHA="${1:-}"
SOURCE_REPO_DIR="${2:-$DEFAULT_SOURCE_REPO_DIR}"

if [ -z "$RELEASE_SHA" ]; then
    fail "usage: deploy_vps_systemd.sh RELEASE_SHA [SOURCE_REPO_DIR]"
fi
if ! printf '%s' "$RELEASE_SHA" | grep -Eq '^[0-9a-f]{7,40}$'; then
    fail "RELEASE_SHA must be a 7-40 char lowercase hex git commit SHA, got: $RELEASE_SHA"
fi

if [ "$(id -u)" -ne 0 ]; then
    fail "must run as root (systemd unit install, /opt and /etc writes require it)"
fi

# ---------------------------------------------------------------------------
# Preflight -- fail fast BEFORE touching anything
# ---------------------------------------------------------------------------
log "preflight: checking Caddy is active (read-only check, never modified by this script)"
if ! systemctl is-active --quiet caddy; then
    fail "Caddy is not active -- refusing to deploy. This script never starts/modifies Caddy; if Caddy is down that is a separate, pre-existing problem to fix first."
fi

log "preflight: checking Barberbot (port ${BARBERBOT_PORT}) is listening -- NO TOUCH, read-only check"
if ! ss -Htln "sport = :${BARBERBOT_PORT}" 2>/dev/null | grep -q "${BARBERBOT_PORT}"; then
    fail "Barberbot's port ${BARBERBOT_PORT} is not in LISTEN state. Per SERVER_DEPLOYMENT_RUNBOOK.md's hard rule (IF ANY BARBERBOT DETAIL IS UNKNOWN/UNVERIFIABLE: DO NOT DEPLOY), refusing to proceed rather than risk deploying against an unknown Barberbot state."
fi

log "preflight: checking port ${CHAMBEANDO_PORT} is free or already owned by ${SERVICE_NAME}"
if ss -Htln "sport = :${CHAMBEANDO_PORT}" 2>/dev/null | grep -q "${CHAMBEANDO_PORT}"; then
    if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
        fail "port ${CHAMBEANDO_PORT} is already in use by something OTHER than ${SERVICE_NAME} (which is not currently active). Refusing to deploy over an unknown listener -- investigate manually first."
    fi
    log "port ${CHAMBEANDO_PORT} is already owned by ${SERVICE_NAME} (this is a redeploy) -- continuing"
else
    log "port ${CHAMBEANDO_PORT} is free -- continuing"
fi

log "preflight: checking source repo and release SHA are available"
if [ ! -d "$SOURCE_REPO_DIR/.git" ]; then
    fail "SOURCE_REPO_DIR ($SOURCE_REPO_DIR) is not a git checkout. Keep a clone of luisdavidv8903-lgtm/finadvisor-pro up to date there separately -- this script never clones/fetches itself."
fi
if ! git -C "$SOURCE_REPO_DIR" cat-file -e "${RELEASE_SHA}^{commit}" 2>/dev/null; then
    fail "commit $RELEASE_SHA not found in $SOURCE_REPO_DIR -- fetch it there first (git fetch), this script does not fetch on your behalf."
fi

log "preflight: checking ${ENV_FILE} already exists (never generated by this script)"
if [ ! -f "$ENV_FILE" ]; then
    fail "$ENV_FILE does not exist. This script NEVER generates or writes secrets -- create it manually first (see chambeando/backend/.env.example and DEPLOYMENT_RUNBOOK.md section C for the required variable names, values never sourced from the repo)."
fi
ENV_PERMS="$(stat -c '%a' "$ENV_FILE")"
if [ "$ENV_PERMS" != "600" ] && [ "$ENV_PERMS" != "400" ]; then
    log "WARNING: $ENV_FILE has permissions $ENV_PERMS (expected 600) -- not changed automatically by this script, tighten it manually (chmod 600 $ENV_FILE)"
fi

log "preflight OK -- proceeding"

# ---------------------------------------------------------------------------
# Release directory (idempotent: reuse if this exact SHA was deployed before)
# ---------------------------------------------------------------------------
mkdir -p "$RELEASES_DIR" "$DATA_DIR"
RELEASE_DIR="${RELEASES_DIR}/${RELEASE_SHA}"

if [ -d "$RELEASE_DIR" ] && [ -f "${RELEASE_DIR}/backend/main.py" ]; then
    log "release directory for $RELEASE_SHA already exists -- reusing (idempotent redeploy)"
else
    log "extracting chambeando/ at $RELEASE_SHA into $RELEASE_DIR"
    rm -rf "$RELEASE_DIR"
    mkdir -p "$RELEASE_DIR"
    # git archive of just the chambeando/ subtree, prefix stripped on extract
    # so RELEASE_DIR ends up laid out exactly like chambeando/ itself
    # (RELEASE_DIR/backend/..., matching the backend.main:app invocation
    # pattern verified in DEPLOYMENT_RUNBOOK.md).
    git -C "$SOURCE_REPO_DIR" archive "$RELEASE_SHA" -- chambeando \
        | tar -x -C "$RELEASE_DIR" --strip-components=1
    if [ ! -f "${RELEASE_DIR}/backend/main.py" ]; then
        fail "extraction did not produce ${RELEASE_DIR}/backend/main.py -- aborting before touching the running service"
    fi
fi

# ---------------------------------------------------------------------------
# Shared venv -- create once, reinstall requirements for this release every time
# ---------------------------------------------------------------------------
if [ ! -x "${VENV_DIR}/bin/python" ]; then
    log "creating venv at $VENV_DIR (python3.12)"
    python3.12 -m venv "$VENV_DIR" || python3 -m venv "$VENV_DIR"
fi

log "installing ${RELEASE_DIR}/backend/requirements.txt into shared venv"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${RELEASE_DIR}/backend/requirements.txt"

# ---------------------------------------------------------------------------
# Alembic migration -- env vars sourced into a subshell only, never printed
# ---------------------------------------------------------------------------
log "running alembic upgrade head (output sanitized -- no env values echoed)"
(
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
    cd "$RELEASE_DIR"
    "${VENV_DIR}/bin/alembic" -c backend/alembic.ini upgrade head
) || fail "alembic upgrade head failed -- service NOT restarted, current symlink NOT moved"
log "migration OK"

# ---------------------------------------------------------------------------
# Atomic 'current' symlink swap
# ---------------------------------------------------------------------------
log "swapping ${CURRENT_LINK} -> ${RELEASE_DIR} (atomic)"
ln -sfn "$RELEASE_DIR" "${CURRENT_LINK}.tmp"
mv -Tf "${CURRENT_LINK}.tmp" "$CURRENT_LINK"

# ---------------------------------------------------------------------------
# systemd unit -- regenerate deterministically, install only if changed
# ---------------------------------------------------------------------------
NEW_UNIT_CONTENT="[Unit]
Description=Chambeando backend
After=network.target

[Service]
Type=simple
WorkingDirectory=${CURRENT_LINK}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/uvicorn backend.main:app --host 127.0.0.1 --port ${CHAMBEANDO_PORT} --no-server-header
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}
StateDirectory=chambeando

[Install]
WantedBy=multi-user.target
"

UNIT_CHANGED=0
if [ ! -f "$SERVICE_FILE" ] || [ "$(cat "$SERVICE_FILE")" != "$NEW_UNIT_CONTENT" ]; then
    UNIT_CHANGED=1
    log "writing $SERVICE_FILE"
    printf '%s' "$NEW_UNIT_CONTENT" > "$SERVICE_FILE"
    chmod 644 "$SERVICE_FILE"
    systemctl daemon-reload
else
    log "systemd unit unchanged -- skipping daemon-reload"
fi

# ---------------------------------------------------------------------------
# Restart and verify
# ---------------------------------------------------------------------------
log "restarting ${SERVICE_NAME}"
systemctl enable --quiet "${SERVICE_NAME}" 2>/dev/null || true
systemctl restart "${SERVICE_NAME}"

log "waiting for ${HEALTH_URL} to respond 200"
HEALTH_OK=0
for _ in $(seq 1 15); do
    if curl -sf -o /dev/null -m 2 "$HEALTH_URL"; then
        HEALTH_OK=1
        break
    fi
    sleep 1
done

if [ "$HEALTH_OK" -ne 1 ]; then
    fail "health check did not pass within 15s after restart -- check 'journalctl -u ${SERVICE_NAME} -n 50' manually. Symlink and unit are already updated; consider rollback_vps_systemd.sh to the previous SHA if this doesn't recover."
fi
log "health check OK: ${HEALTH_URL}"

# ---------------------------------------------------------------------------
# Post-deploy safety verification -- Barberbot and Caddy must be unaffected
# ---------------------------------------------------------------------------
log "post-deploy: verifying Barberbot (port ${BARBERBOT_PORT}) is still listening"
if ! ss -Htln "sport = :${BARBERBOT_PORT}" 2>/dev/null | grep -q "${BARBERBOT_PORT}"; then
    fail "Barberbot's port ${BARBERBOT_PORT} is NOT listening after this deploy. This script never touched Barberbot directly, but treat this as a critical incident and investigate immediately -- do not assume it is unrelated."
fi
log "Barberbot OK -- still listening on ${BARBERBOT_PORT}"

log "post-deploy: verifying Caddy is still active"
if ! systemctl is-active --quiet caddy; then
    fail "Caddy is not active after this deploy. This script never touched Caddy config or reloaded it, but treat this as a critical incident and investigate immediately."
fi
log "Caddy OK -- still active"

log "DEPLOY OK -- release ${RELEASE_SHA} is live at ${CURRENT_LINK}, unit_changed=${UNIT_CHANGED}"
