#!/usr/bin/env bash
# Usage: reply.sh <chat_id> <message_id> <text>  -- reply as the bot; queued for retry on failure.
set -u
ENV_FILE="${BRIDGE_ENV_FILE:-/etc/feishu-grokbot-bridge.env}"
# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
[ $# -ge 3 ] || { echo "usage: $0 <chat_id> <message_id> <text>" >&2; exit 2; }
CHAT="$1"; MID="$2"; TEXT="$3"
OUT="${BRIDGE_WORK_DIR:-/var/lib/feishu-grokbot-bridge}/outbox"; LARK="${LARK_CLI:-lark-cli}"
IDEM="r-$(date +%s%N | cut -c1-16)-$RANDOM"
# shellcheck disable=SC2086
if "$LARK" ${LARK_CLI_EXTRA_ARGS:-} im +messages-reply --as bot --message-id "$MID" --text "$TEXT" --idempotency-key "$IDEM"; then exit 0; fi
mkdir -p "$OUT"
python3 - "$CHAT" "$MID" "$TEXT" "$IDEM" "$OUT" <<'PY'
import json,sys,time,os
c,m,t,i,o=sys.argv[1:]; n=f"{int(time.time()*1000)}-{i[-6:]}.json"
json.dump({"chat_id":c,"message_id":m,"text":t,"idem":i,"attempts":0},open(f"{o}/.{n}","w"),ensure_ascii=False); os.replace(f"{o}/.{n}",f"{o}/{n}")
PY
echo "reply failed; queued to $OUT" >&2; exit 1
