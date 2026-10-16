#!/usr/bin/env bash
# Fallback for hosts without systemd: run the forwarder in a restart loop (exit -> restart after 3s).
ENV_FILE="${1:-${BRIDGE_ENV_FILE:-/etc/feishu-grokbot-bridge.env}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
WORK="${BRIDGE_WORK_DIR:-/var/lib/feishu-grokbot-bridge}"; mkdir -p "$WORK"
if [ -f "$WORK/loop.pid" ] && kill -0 "$(cat "$WORK/loop.pid")" 2>/dev/null; then echo "already running"; exit 0; fi
export BRIDGE_LOG_FILE="${BRIDGE_LOG_FILE:-$WORK/forwarder.log}" PYTHONUNBUFFERED=1
nohup bash -c "while true; do python3 '$ROOT/bridge/forwarder.py' '$ENV_FILE'; sleep 3; done" >>"$WORK/forwarder.out" 2>&1 </dev/null &
echo $! > "$WORK/loop.pid"; echo "started loop pid $!; log: $BRIDGE_LOG_FILE"
