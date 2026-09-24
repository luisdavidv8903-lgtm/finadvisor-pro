#!/usr/bin/env bash
#
# verify_vps_systemd.sh -- READ-ONLY health/status check for the Chambeando
# systemd deployment. Never restarts, edits, or modifies anything -- safe to
# run as often as desired (e.g. from a monitoring cron), and safe to run as
# a non-root user for everything except MemoryCurrent/Peak (which needs
# `systemctl show`, available to any user for a system unit's public
# properties -- no root required for read-only systemctl queries).
#
# Usage:
#   verify_vps_systemd.sh
#
# Never prints the contents of /etc/chambeando/chambeando.env or any other
# secret -- only existence and permission bits are checked.
#
# Exits 0 and prints VERIFY_GATE=PASS if every check passes.
# Exits 1 and prints VERIFY_GATE=FAIL otherwise (still runs every check
# first and reports all of them, rather than stopping at the first failure).

set -uo pipefail
# Deliberately NOT `set -e`: this script must run every check and report a
# full picture even if an early one fails -- a single missing tool (e.g. no
# `ss` on a minimal system) should not abort the whole read-only report.

BASE_DIR="/opt/chambeando"
CURRENT_LINK="${BASE_DIR}/current"
DATA_DIR="/var/lib/chambeando"
ENV_FILE="/etc/chambeando/chambeando.env"
SERVICE_NAME="chambeando.service"
CHAMBEANDO_PORT="8100"
BARBERBOT_PORT="3001"
HEALTH_URL="http://127.0.0.1:${CHAMBEANDO_PORT}/health"

PASS=1
note() { printf '%s\n' "$1"; }
check_ok() { printf '[OK]   %s\n' "$1"; }
check_fail() { printf '[FAIL] %s\n' "$1"; PASS=0; }
check_warn() { printf '[WARN] %s\n' "$1"; }

note "== chambeando.service =="
if systemctl is-active --quiet "$SERVICE_NAME"; then
    check_ok "${SERVICE_NAME} is active"
else
    check_fail "${SERVICE_NAME} is NOT active"
fi

note ""
note "== health endpoint =="
HTTP_CODE="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$HEALTH_URL" 2>/dev/null || echo "000")"
if [ "$HTTP_CODE" = "200" ]; then
    check_ok "GET ${HEALTH_URL} -> 200"
else
    check_fail "GET ${HEALTH_URL} -> ${HTTP_CODE} (expected 200)"
fi

note ""
note "== binding =="
if command -v ss >/dev/null 2>&1; then
    BIND_LINE="$(ss -Htln "sport = :${CHAMBEANDO_PORT}" 2>/dev/null || true)"
    if printf '%s' "$BIND_LINE" | grep -q "127.0.0.1:${CHAMBEANDO_PORT}"; then
        check_ok "listening on 127.0.0.1:${CHAMBEANDO_PORT} only"
    elif printf '%s' "$BIND_LINE" | grep -qE "0\.0\.0\.0:${CHAMBEANDO_PORT}|\*:${CHAMBEANDO_PORT}|\[::\]:${CHAMBEANDO_PORT}"; then
        check_fail "listening on ALL interfaces on port ${CHAMBEANDO_PORT}, not just 127.0.0.1 -- this must stay localhost-only, Caddy is the only intended public entry point"
    else
        check_fail "nothing found listening on port ${CHAMBEANDO_PORT}"
    fi
else
    check_warn "'ss' not available -- could not verify binding"
fi

note ""
note "== resource usage (chambeando.service) =="
NRESTARTS="$(systemctl show "$SERVICE_NAME" -p NRestarts --value 2>/dev/null || echo "unknown")"
MEM_CURRENT="$(systemctl show "$SERVICE_NAME" -p MemoryCurrent --value 2>/dev/null || echo "unknown")"
MEM_PEAK="$(systemctl show "$SERVICE_NAME" -p MemoryPeak --value 2>/dev/null || echo "unknown")"
note "NRestarts=${NRESTARTS}"
if [ "$MEM_CURRENT" != "unknown" ] && [ "$MEM_CURRENT" != "[not set]" ]; then
    note "MemoryCurrent=$(( MEM_CURRENT / 1024 / 1024 ))MB (raw: ${MEM_CURRENT} bytes)"
else
    note "MemoryCurrent=unknown"
fi
if [ "$MEM_PEAK" != "unknown" ] && [ "$MEM_PEAK" != "[not set]" ]; then
    note "MemoryPeak=$(( MEM_PEAK / 1024 / 1024 ))MB (raw: ${MEM_PEAK} bytes)"
else
    note "MemoryPeak=unknown"
fi
if [ "$NRESTARTS" != "unknown" ] && [ "$NRESTARTS" -gt 0 ] 2>/dev/null; then
    check_warn "NRestarts=${NRESTARTS} (nonzero -- service has restarted at least once since last reset; not necessarily a failure, but worth investigating via 'journalctl -u ${SERVICE_NAME}')"
else
    check_ok "NRestarts=${NRESTARTS}"
fi

note ""
note "== recent journal warnings/errors (last 5 minutes, sanitized) =="
if command -v journalctl >/dev/null 2>&1; then
    RECENT_ERRORS="$(journalctl -u "$SERVICE_NAME" --since '5 minutes ago' -p warning --no-pager 2>/dev/null | wc -l)"
    if [ "$RECENT_ERRORS" -eq 0 ] 2>/dev/null; then
        check_ok "no warning/error lines in the last 5 minutes"
    else
        check_warn "${RECENT_ERRORS} warning/error line(s) in the journal in the last 5 minutes -- run 'journalctl -u ${SERVICE_NAME} --since \"5 minutes ago\" -p warning' manually to review (not printed here to avoid leaking anything sensitive that might be in a log line)"
    fi
else
    check_warn "'journalctl' not available -- could not check recent logs"
fi

note ""
note "== Caddy (NO TOUCH -- read-only check) =="
if systemctl is-active --quiet caddy; then
    check_ok "caddy is active"
else
    check_fail "caddy is NOT active"
fi

note ""
note "== Barberbot (NO TOUCH -- read-only check, port ${BARBERBOT_PORT}) =="
if command -v ss >/dev/null 2>&1; then
    if ss -Htln "sport = :${BARBERBOT_PORT}" 2>/dev/null | grep -q "${BARBERBOT_PORT}"; then
        check_ok "port ${BARBERBOT_PORT} is listening"
    else
        check_fail "port ${BARBERBOT_PORT} is NOT listening"
    fi
else
    check_warn "'ss' not available -- could not verify Barberbot's port"
fi

note ""
note "== disk =="
if command -v df >/dev/null 2>&1; then
    df -h / "$DATA_DIR" 2>/dev/null | tail -n +1
else
    check_warn "'df' not available"
fi

note ""
note "== memory (system-wide) =="
if command -v free >/dev/null 2>&1; then
    free -h
else
    check_warn "'free' not available"
fi

note ""
note "== current release =="
if [ -L "$CURRENT_LINK" ]; then
    TARGET="$(readlink -f "$CURRENT_LINK")"
    check_ok "current -> ${TARGET}"
else
    check_fail "${CURRENT_LINK} is not a symlink (or does not exist)"
fi

note ""
note "== SQLite database =="
DB_FOUND="$(find "$DATA_DIR" -maxdepth 1 -iname '*.db' 2>/dev/null | head -1)"
if [ -n "$DB_FOUND" ]; then
    check_ok "database file present: $(basename "$DB_FOUND") ($(stat -c '%s' "$DB_FOUND" 2>/dev/null || echo '?') bytes)"
else
    check_fail "no *.db file found under ${DATA_DIR}"
fi

note ""
note "== permissions (existence/mode only, no contents ever printed) =="
if [ -f "$ENV_FILE" ]; then
    PERMS="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo "?")"
    if [ "$PERMS" = "600" ] || [ "$PERMS" = "400" ]; then
        check_ok "${ENV_FILE} exists with mode ${PERMS}"
    else
        check_warn "${ENV_FILE} exists but mode is ${PERMS} (expected 600) -- tighten manually, this script never modifies it"
    fi
else
    check_fail "${ENV_FILE} does not exist"
fi
if [ -d "$DATA_DIR" ]; then
    DATA_PERMS="$(stat -c '%a' "$DATA_DIR" 2>/dev/null || echo "?")"
    check_ok "${DATA_DIR} exists (mode ${DATA_PERMS})"
else
    check_fail "${DATA_DIR} does not exist"
fi

note ""
if [ "$PASS" -eq 1 ]; then
    note "VERIFY_GATE=PASS"
    exit 0
else
    note "VERIFY_GATE=FAIL"
    exit 1
fi
