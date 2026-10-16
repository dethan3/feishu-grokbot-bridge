#!/usr/bin/env bash
. "$(dirname "$0")/common.sh"
cd "$WORK"
if ! flock -n forwarder.lock true 2>/dev/null; then echo "already running (pid $(cat forwarder.pid 2>/dev/null))"; exit 0; fi
PYTHONPATH="$ROOT" nohup "$PY" -m bridge.forwarder --env-file "$ENV_FILE" --work-dir "$WORK" >>forwarder.out 2>&1 </dev/null &
sleep 1; echo "started pid $! (work dir $WORK)"
