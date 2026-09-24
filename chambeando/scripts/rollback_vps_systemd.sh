#!/usr/bin/env bash
#
# rollback_vps_systemd.sh -- roll Chambeando's `current` symlink back to a
# previously-deployed release and restart ONLY chambeando.service.
#
# Intended to run ON THE VPS as root, same environment as
# deploy_vps_systemd.sh (see SERVER_DEPLOYMENT_RUNBOOK.md section 8).
#
# Usage:
#   rollback_vps_systemd.sh RELEASE_SHA
#
#   RELEASE_SHA   A commit SHA that already has a release directory under
#                 /opt/chambeando/releases/ (i.e. it was deployed before via
#                 deploy_vps_systemd.sh). This script does NOT fetch, build,
#                 or create a new release -- it only re-points to one that
#                 already exists on disk.
#
# What this script explicitly does NOT do:
#   - touch /etc/chambeando/chambeando.env
#   - touch the database (no migration run/reverted -- see the warning below)
#   - touch Caddy, DNS, Meta, or Telegram
#   - touch Barberbot in any way
#   - restart anything other than chambeando.service
#
# IMPORTANT: rolling back CODE does not roll back DATABASE MIGRATIONS. If the
# release being rolled back FROM applied an Alembic migration that the
# release being rolled back TO does not expect, this script will NOT revert
# that migration. Check `alembic history` and decide manually whether an
# `alembic downgrade` is also needed BEFORE running this -- this script
# deliberately never runs one automatically (an automatic downgrade against
# a real database is exactly the kind of action that needs a human looking
# at it first).

set -euo pipefail

BASE_DIR="/opt/chambeando"
RELEASES_DIR="${BASE_DIR}/releases"
CURRENT_LINK="${BASE_DIR}/current"
SERVICE_NAME="chambeando.service"
CHAMBEANDO_PORT="8100"
BARBERBOT_PORT="3001"
HEALTH_URL="http://127.0.0.1:${CHAMBEANDO_PORT}/health"

log() { printf '[rollback] %s\n' "$1"; }
fail() { printf '[rollback] FAILED: %s\n' "$1" >&2; exit 1; }

trap 'printf "[rollback] FAILED at line %s.\n" "$LINENO" >&2' ERR

RELEASE_SHA="${1:-}"
if [ -z "$RELEASE_SHA" ]; then
    fail "usage: rollback_vps_systemd.sh RELEASE_SHA"
fi
if ! printf '%s' "$RELEASE_SHA" | grep -Eq '^[0-9a-f]{7,40}$'; then
    fail "RELEASE_SHA must be a 7-40 char lowercase hex git commit SHA, got: $RELEASE_SHA"
fi
if [ "$(id -u)" -ne 0 ]; then
    fail "must run as root (systemd restart and /opt symlink write require it)"
fi

RELEASE_DIR="${RELEASES_DIR}/${RELEASE_SHA}"
if [ ! -d "$RELEASE_DIR" ] || [ ! -f "${RELEASE_DIR}/backend/main.py" ]; then
    fail "release $RELEASE_SHA does not exist under $RELEASES_DIR (or looks incomplete) -- this script only re-points to an already-deployed release, it does not create a new one. Run deploy_vps_systemd.sh first if you need to deploy this SHA."
fi

CURRENT_TARGET="$(readlink -f "$CURRENT_LINK" 2>/dev/null || echo "")"
if [ "$CURRENT_TARGET" = "$RELEASE_DIR" ]; then
    log "current already points at $RELEASE_SHA -- nothing to change, will still verify health/Barberbot/Caddy below"
else
    log "reminder: rolling back CODE only. This does NOT revert any database migration -- check 'alembic history' manually first if the release you're rolling back FROM applied one that this release doesn't expect."
    log "swapping ${CURRENT_LINK} -> ${RELEASE_DIR} (atomic)"
    ln -sfn "$RELEASE_DIR" "${CURRENT_LINK}.tmp"
    mv -Tf "${CURRENT_LINK}.tmp" "$CURRENT_LINK"
fi

log "restarting ${SERVICE_NAME} only (env, DB, Caddy, Barberbot untouched)"
if ! systemctl restart "${SERVICE_NAME}"; then
    fail "systemctl restart ${SERVICE_NAME} failed -- current symlink was already moved to $RELEASE_SHA, but the service did not come up. Check 'journalctl -u ${SERVICE_NAME} -n 50' immediately."
fi

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
    fail "health check did not pass within 15s after rollback restart -- the symlink now points at $RELEASE_SHA and the service was restarted, but it is not answering /health. Check 'journalctl -u ${SERVICE_NAME} -n 50' manually; this script will not retry further automatically."
fi
log "health check OK: ${HEALTH_URL}"

log "verifying Barberbot (port ${BARBERBOT_PORT}) is still listening"
if ! ss -Htln "sport = :${BARBERBOT_PORT}" 2>/dev/null | grep -q "${BARBERBOT_PORT}"; then
    fail "Barberbot's port ${BARBERBOT_PORT} is NOT listening after this rollback. This script never touched Barberbot, but treat this as a critical incident and investigate immediately."
fi
log "Barberbot OK -- still listening on ${BARBERBOT_PORT}"

log "verifying Caddy is still active"
if ! systemctl is-active --quiet caddy; then
    fail "Caddy is not active after this rollback. This script never touched Caddy, but treat this as a critical incident and investigate immediately."
fi
log "Caddy OK -- still active"

log "ROLLBACK OK -- ${CURRENT_LINK} now points at ${RELEASE_SHA}"
