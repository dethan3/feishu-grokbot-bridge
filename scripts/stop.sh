#!/usr/bin/env bash
. "$(dirname "$0")/common.sh"
P=$(cat "$WORK/forwarder.pid" 2>/dev/null)
[ -n "$P" ] && kill -0 "$P" 2>/dev/null || { echo "not running"; exit 0; }
pkill -TERM -P "$P"; kill -TERM "$P"; echo "stopped $P"
