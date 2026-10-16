#!/usr/bin/env bash
# Usage: BRIDGE_ENV_FILE=.env scripts/reply.sh <chat_id> <message_id> <text>
# Reply as the bot; on failure queue to <work>/outbox/ (retried by the forwarder).
set -u
[ $# -ge 3 ] || { echo "usage: $0 <chat_id> <message_id> <text>" >&2; exit 2; }
CHAT="$1"; MID="$2"; TEXT="$3"; set --
. "$(dirname "$0")/common.sh"
IDEM="r-$(date +%s%N | cut -c1-16)-$RANDOM"
# shellcheck disable=SC2086
"$LARK" ${LARK_CLI_EXTRA_ARGS:-} im +messages-reply --as bot --message-id "$MID" --text "$TEXT" --idempotency-key "$IDEM" && exit 0
PYTHONPATH="$ROOT" "$PY" -c 'import sys; from bridge.forwarder import Queue; c,m,t,i,o=sys.argv[1:]; Queue(o).put({"chat_id":c,"message_id":m,"text":t,"idem":i,"attempts":0})' \
  "$CHAT" "$MID" "$TEXT" "$IDEM" "$WORK/outbox"
echo "reply failed; queued to $WORK/outbox" >&2; exit 1
