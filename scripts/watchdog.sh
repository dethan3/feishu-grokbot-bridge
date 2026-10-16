#!/usr/bin/env bash
# Keeps one forwarder instance alive: health check every 60s, preventive reconnect every 30 min.
. "$(dirname "$0")/common.sh"
cd "$WORK"
exec 9>watchdog.lock; flock -n 9 || { echo "watchdog already running for $WORK"; exit 0; }
echo $$ > watchdog.pid
CHECK=${WATCHDOG_CHECK_EVERY:-60}; RECONNECT=${WATCHDOG_RECONNECT_EVERY:-1800}
log(){ echo "$(date '+%F %T') WATCHDOG $*" >> forwarder.log; }
restart(){ log "restart: $1"; "$ROOT/scripts/stop.sh" "$ENV_FILE" >/dev/null 2>&1; sleep 3; "$ROOT/scripts/start.sh" "$ENV_FILE" >/dev/null 2>&1; last=$(date +%s); }
last=$(date +%s)
while true; do
  sleep "$CHECK"
  pid=$(cat forwarder.pid 2>/dev/null)
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then restart "forwarder dead"; continue; fi
  # shellcheck disable=SC2086
  st=$("$LARK" $LARK_CLI_EXTRA_ARGS event status 2>&1)
  if ! grep -q "Bus: *running" <<<"$st" || ! grep -q "Active consumers: *[1-9]" <<<"$st"; then restart "bus/consumer down"; continue; fi
  if [ "$RECONNECT" -gt 0 ] && [ $(( $(date +%s) - last )) -ge "$RECONNECT" ]; then restart "preventive reconnect"; fi
done
