#!/usr/bin/env bash
# Fallback stop for scripts/start.sh (systemd hosts: systemctl stop feishu-grokbot-bridge)
ENV_FILE="${1:-${BRIDGE_ENV_FILE:-/etc/feishu-grokbot-bridge.env}}"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
WORK="${BRIDGE_WORK_DIR:-/var/lib/feishu-grokbot-bridge}"
L=$(cat "$WORK/loop.pid" 2>/dev/null); P=$(cat "$WORK/forwarder.pid" 2>/dev/null)
[ -n "$L" ] && kill "$L" 2>/dev/null
[ -n "$P" ] && { pkill -TERM -P "$P"; kill -TERM "$P"; } 2>/dev/null
"${LARK_CLI:-lark-cli}" event stop >/dev/null 2>&1; echo "stopped"
