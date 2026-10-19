#!/usr/bin/env bash
# feishu-grokbot-bridge one-click installer (Ubuntu/Debian/CentOS/Alibaba Cloud Linux; apt/dnf/yum).
#   curl -fsSL https://raw.githubusercontent.com/dethan3/feishu-grokbot-bridge/main/install.sh | sudo bash
#   ... | sudo bash -s -- --instance xiaoi --app-name 小i --guest-p2p    # extra instance for another Feishu app
# Options (env vars or flags):
#   --instance NAME           instance name (default: "default" = original paths/service name)
#   --app-name NAME           value of "app" in webhook payload (default: instance name)
#   --guest-p2p / --no-guest-p2p  forward non-owner direct messages (default off; kept on re-run)
#   --dry-run / DRY_RUN=1     print what would be done, change nothing
#   --no-start                install but do not start the service
#   GH_PROXY=https://mirror/  prefix for github.com downloads (if GitHub is slow from mainland China)
#   Preset answers to skip prompts: FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_OWNER_OPEN_IDS FEISHU_BOT_OPEN_ID
#                                   FEISHU_WEBHOOK_URL FEISHU_WEBHOOK_KEY
set -euo pipefail

REPO="${BRIDGE_REPO:-dethan3/feishu-grokbot-bridge}"
BRANCH="${BRIDGE_BRANCH:-main}"
LARK_TAG="${LARK_CLI_TAG:-lark-cli-v1.0.98-sdk3.12.2}"
BASE=feishu-grokbot-bridge
GH_PROXY="${GH_PROXY:-}"
DRY_RUN="${DRY_RUN:-0}"; NO_START=0
INSTANCE="${BRIDGE_INSTANCE:-}"; APP_NAME_ARG="${FEISHU_APP_NAME:-}"; GUEST_P2P_ARG="${FEISHU_GUEST_P2P:-}"
while [ $# -gt 0 ]; do case "$1" in
  --dry-run) DRY_RUN=1;; --no-start) NO_START=1;;
  --instance) INSTANCE="${2:-}"; shift;; --instance=*) INSTANCE="${1#*=}";;
  --app-name) APP_NAME_ARG="${2:-}"; shift;; --app-name=*) APP_NAME_ARG="${1#*=}";;
  --guest-p2p) GUEST_P2P_ARG=1;; --no-guest-p2p) GUEST_P2P_ARG=0;;
  -h|--help) sed -n '2,16p' "$0" 2>/dev/null || true; exit 0;;
  *) echo "unknown option: $1" >&2; exit 2;; esac; shift; done
INSTANCE="${INSTANCE:-default}"
[[ "$INSTANCE" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]] || { echo "bad --instance '$INSTANCE' (a-z 0-9 _ -)" >&2; exit 2; }
ROOT="${BRIDGE_ROOT:-}"   # test hook: install under a fake root (no systemd actions)
PREFIX="${BRIDGE_PREFIX:-$ROOT/opt/$BASE}"
LARK_BIN="${LARK_BIN:-$ROOT/usr/local/bin/lark-cli}"
UNIT_DIR="${BRIDGE_UNIT_DIR:-$ROOT/etc/systemd/system}"
if [ "$INSTANCE" = default ]; then
  NAME=$BASE
  ENV_FILE="${BRIDGE_ENV_FILE:-$ROOT/etc/$BASE.env}"
  WORK="${BRIDGE_WORK_DIR:-$ROOT/var/lib/$BASE}"
else
  NAME=$BASE-$INSTANCE
  ENV_FILE="${BRIDGE_ENV_FILE:-$ROOT/etc/$NAME.env}"
  WORK="${BRIDGE_WORK_DIR:-$ROOT/var/lib/$BASE/$INSTANCE}"
fi
UNIT="$UNIT_DIR/$NAME.service"

c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_0=$'\e[0m'
info(){ echo "${c_g}==>${c_0} $*"; }
warn(){ echo "${c_y}!!${c_0} $*" >&2; }
die(){ echo "${c_r}xx${c_0} $*" >&2; exit 1; }
run(){ if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

# ---------- prompts (work under `curl | bash` by reading /dev/tty) ----------
TTY=/dev/tty
have_tty(){ [ -r "$TTY" ] && { : <"$TTY"; } 2>/dev/null; }
ask(){ # ask VAR "prompt" [default] [secret]
  local var=$1 prompt=$2 def=${3:-} secret=${4:-} ans="" shown
  if [ -n "${!var:-}" ]; then return 0; fi
  if ! have_tty; then
    [ -n "$def" ] && { printf -v "$var" '%s' "$def"; return 0; }
    die "no terminal for prompt '$prompt'; preset $var=... in the environment"
  fi
  if [ -n "$secret" ]; then shown=${def:+[回车保留现有值]}; else shown=${def:+[$def]}; fi
  while :; do
    if [ -n "$secret" ]; then read -r -s -p "$prompt $shown: " ans <"$TTY"; echo >"$TTY"
    else read -r -p "$prompt $shown: " ans <"$TTY"; fi
    ans=${ans:-$def}
    [ -n "$ans" ] && break; echo "  不能为空 / required" >"$TTY"
  done
  printf -v "$var" '%s' "$ans"
}
validate(){ # validate VAR regex hint
  [[ "${!1}" =~ $2 ]] || die "$1 格式不对 ($3)"; }

# ---------- checks ----------
[ "$(uname -s)" = Linux ] || die "Linux only"
if [ "$(id -u)" != 0 ] && [ "$DRY_RUN" != 1 ] && [ -z "$ROOT" ]; then die "请用 root 运行：curl -fsSL ... | sudo bash"; fi
case "$(uname -m)" in x86_64|amd64) ARCH=amd64;; aarch64|arm64) ARCH=arm64;; *) die "unsupported arch $(uname -m)";; esac
HAS_SYSTEMD=0; [ -z "$ROOT" ] && [ -d /run/systemd/system ] && command -v systemctl >/dev/null && HAS_SYSTEMD=1
info "instance=$INSTANCE service=$NAME arch=$ARCH systemd=$HAS_SYSTEMD dry_run=$DRY_RUN"

# ---------- packages ----------
need=(); for c in python3 curl tar gzip flock sha256sum; do command -v "$c" >/dev/null || need+=("$c"); done
if [ ${#need[@]} -gt 0 ]; then
  info "installing packages for: ${need[*]}"
  if command -v apt-get >/dev/null; then
    run env DEBIAN_FRONTEND=noninteractive apt-get update -y
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 curl tar gzip util-linux coreutils ca-certificates
  elif command -v dnf >/dev/null; then run dnf install -y python3 curl tar gzip util-linux coreutils ca-certificates
  elif command -v yum >/dev/null; then run yum install -y python3 curl tar gzip util-linux coreutils ca-certificates
  else die "no apt-get/dnf/yum; install manually: ${need[*]}"; fi
fi
if [ "$DRY_RUN" != 1 ]; then
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 6))' || die "python3 >= 3.6 required"
fi

# ---------- code ----------
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
SELF_DIR=""; [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] && SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/bridge/forwarder.py" ]; then
  SRC="$SELF_DIR"; info "using local source $SRC"
else
  url="${BRIDGE_TARBALL:-${GH_PROXY}https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz}"
  info "downloading $url"
  if [ -f "$url" ]; then cp "$url" "$TMP/src.tgz"; else curl -fsSL --retry 3 "$url" -o "$TMP/src.tgz" || die "download failed (try GH_PROXY=...)"; fi
  tar --no-same-owner -xzf "$TMP/src.tgz" -C "$TMP"; SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
fi
run mkdir -p "$PREFIX" "$WORK" "$(dirname "$LARK_BIN")" "$(dirname "$ENV_FILE")"
run cp -r "$SRC/bridge" "$SRC/scripts" "$PREFIX/"
[ -f "$SRC/install.sh" ] && run cp "$SRC/install.sh" "$PREFIX/"
[ -f "$SRC/.env.example" ] && run cp "$SRC/.env.example" "$PREFIX/"
run chmod 700 "$WORK"

# ---------- lark-cli (patched build: lark-cli 1.0.98 + oapi-sdk-go v3.12.2) ----------
install_lark(){
  local gz="$TMP/lark-cli.gz" f="lark-cli-linux-$ARCH.gz" sums="$SRC/bin/SHA256SUMS"
  if [ -f "$SRC/bin/$f" ]; then cp "$SRC/bin/$f" "$gz"
  else curl -fsSL --retry 3 "${GH_PROXY}https://github.com/$REPO/releases/download/$LARK_TAG/$f" -o "$gz" || rm -f "$gz"; fi
  if [ -s "$gz" ]; then
    if [ -f "$sums" ]; then (cd "$(dirname "$gz")" && grep " $f\$" "$sums" | sed "s# $f\$# lark-cli.gz#" | sha256sum -c --quiet -) || die "lark-cli checksum mismatch"; fi
    # write next to target then rename, so a lark-cli used by another running instance is never half-written
    gunzip -c "$gz" > "$TMP/lark-cli" && install -m 755 "$TMP/lark-cli" "$LARK_BIN.new" && mv -f "$LARK_BIN.new" "$LARK_BIN" && return 0
  fi
  warn "patched lark-cli unavailable; falling back to official npm @larksuite/cli (no ws read-deadline patch)"
  command -v npm >/dev/null || { if command -v apt-get >/dev/null; then apt-get install -y nodejs npm; else (dnf install -y nodejs npm || yum install -y nodejs npm); fi; }
  npm install -g @larksuite/cli
  ln -sf "$(command -v lark-cli)" "$LARK_BIN" 2>/dev/null || true
}
info "installing lark-cli -> $LARK_BIN"
if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] install lark-cli-linux-$ARCH (bundled bin/ -> release $LARK_TAG -> npm)"; else install_lark; fi
[ "$DRY_RUN" = 1 ] || "$LARK_BIN" --version

# ---------- answers ----------
# shellcheck disable=SC1090
OLD_APP=""; OLD_OWN=""; OLD_BOT=""; OLD_URL=""; OLD_KEY=""; OLD_NAME=""; OLD_GP2P=""
if [ -r "$ENV_FILE" ]; then
  info "found existing $ENV_FILE (回车保留旧值)"
  OLD_APP=$(sed -n 's/^FEISHU_APP_ID=//p' "$ENV_FILE"); OLD_OWN=$(sed -n 's/^FEISHU_OWNER_OPEN_IDS=//p' "$ENV_FILE")
  OLD_BOT=$(sed -n 's/^FEISHU_BOT_OPEN_ID=//p' "$ENV_FILE"); OLD_URL=$(sed -n 's/^FEISHU_WEBHOOK_URL=//p' "$ENV_FILE")
  OLD_KEY=$(sed -n 's/^FEISHU_WEBHOOK_KEY=//p' "$ENV_FILE")
  OLD_NAME=$(sed -n 's/^FEISHU_APP_NAME=//p' "$ENV_FILE"); OLD_GP2P=$(sed -n 's/^FEISHU_GUEST_P2P=//p' "$ENV_FILE")
fi
DEF_NAME=$INSTANCE; [ "$INSTANCE" = default ] && DEF_NAME=""   # empty -> payload "app" falls back to App ID
FEISHU_APP_NAME="${APP_NAME_ARG:-${OLD_NAME:-$DEF_NAME}}"
FEISHU_GUEST_P2P="${GUEST_P2P_ARG:-${OLD_GP2P:-0}}"
LCFG="$WORK/lark-cli"
echo; echo "请准备：App ID / App Secret（飞书开放平台 → 应用 → 凭证与基础信息）、你的 open_id、webhook URL 与 key"
ask FEISHU_APP_ID "飞书 App ID (cli_...)" "$OLD_APP"; validate FEISHU_APP_ID '^cli_[A-Za-z0-9]+$' "cli_..."
for other in "$ROOT"/etc/$BASE.env "$ROOT"/etc/$BASE-*.env; do
  [ -r "$other" ] && [ "$other" != "$ENV_FILE" ] || continue
  [ "$(sed -n 's/^FEISHU_APP_ID=//p' "$other")" = "$FEISHU_APP_ID" ] && die "App $FEISHU_APP_ID 已被 $other 使用；一个飞书应用只能有一个实例"
done
SECRET_DEF=""; [ -n "$OLD_APP" ] && [ "$OLD_APP" = "$FEISHU_APP_ID" ] && [ -f "$LCFG/config.json" ] && SECRET_DEF="__keep__"
ask FEISHU_APP_SECRET "飞书 App Secret（输入不显示）" "$SECRET_DEF" secret

export LARKSUITE_CLI_CONFIG_DIR="$LCFG"
if [ "$FEISHU_APP_SECRET" != "__keep__" ]; then
  info "configuring lark-cli (config dir $LCFG)"
  if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] printf '<secret>' | lark-cli config init --app-id $FEISHU_APP_ID --app-secret-stdin --brand feishu"
  else mkdir -p "$LCFG"; chmod 700 "$LCFG"
    tries=0
    until printf '%s' "$FEISHU_APP_SECRET" | "$LARK_BIN" config init --app-id "$FEISHU_APP_ID" --app-secret-stdin --brand feishu >"$TMP/init.log" 2>&1; do
      sed -n 's/.*"message": *"\(.*\)".*/  \1/p' "$TMP/init.log" >&2
      tries=$((tries+1)); { have_tty && [ $tries -lt 3 ]; } || die "lark-cli config init failed: App ID / App Secret 不正确？"
      warn "App ID 或 App Secret 校验失败，请重新输入"; FEISHU_APP_SECRET=""
      ask FEISHU_APP_SECRET "飞书 App Secret（输入不显示）" "" secret
    done
  fi
fi
unset FEISHU_APP_SECRET

DETECTED_BOT=""
if [ "$DRY_RUN" != 1 ]; then
  out=$("$LARK_BIN" api GET /open-apis/bot/v3/info --as bot 2>&1 || true)
  DETECTED_BOT=$(printf '%s' "$out" | python3 -c 'import json,sys,re
t=sys.stdin.read(); m=re.search(r"\"open_id\":\s*\"(ou_[A-Za-z0-9]+)\"",t); print(m.group(1) if m else "")' || true)
  if [ -n "$DETECTED_BOT" ]; then info "App 凭证有效；机器人 open_id = $DETECTED_BOT"
  else warn "无法用 App 凭证获取机器人信息（凭证错误或未开启机器人能力？）："; printf '%s\n' "$out" | tail -5 >&2; fi
fi
ask FEISHU_OWNER_OPEN_IDS "主人 open_id（ou_...，多个用逗号分隔）" "$OLD_OWN"
validate FEISHU_OWNER_OPEN_IDS '^ou_[A-Za-z0-9]+( *, *ou_[A-Za-z0-9]+)*$' "ou_...[,ou_...]"
ask FEISHU_BOT_OPEN_ID "机器人 open_id（ou_...）" "${DETECTED_BOT:-$OLD_BOT}"; validate FEISHU_BOT_OPEN_ID '^ou_[A-Za-z0-9]+$' "ou_..."
ask FEISHU_WEBHOOK_URL "Grok Bot webhook URL" "$OLD_URL"; validate FEISHU_WEBHOOK_URL '^https?://[^[:space:]]+$' "http(s)://..."
ask FEISHU_WEBHOOK_KEY "Grok Bot webhook key（输入不显示）" "$OLD_KEY" secret

# ---------- env file ----------
info "writing $ENV_FILE (chmod 600)"
write_env(){
  umask 077
  cat >"$1" <<ENV
# Written by install.sh $(date '+%F %T'). Keep private (chmod 600).
FEISHU_APP_ID=$FEISHU_APP_ID
FEISHU_OWNER_OPEN_IDS=$FEISHU_OWNER_OPEN_IDS
FEISHU_BOT_OPEN_ID=$FEISHU_BOT_OPEN_ID
FEISHU_WEBHOOK_URL=$FEISHU_WEBHOOK_URL
FEISHU_WEBHOOK_KEY=$FEISHU_WEBHOOK_KEY
FEISHU_APP_NAME=$FEISHU_APP_NAME
FEISHU_GUEST_P2P=$FEISHU_GUEST_P2P
BRIDGE_WORK_DIR=$WORK
LARKSUITE_CLI_CONFIG_DIR=$LCFG
LARK_CLI=$LARK_BIN
ENV
  grep -E '^(FEISHU_SEED_CHATS|POLL_|STALE_SEC|RETRY_EVERY|SEEN_MAX|WATCHDOG_|BRIDGE_LOG_FILE)' "$ENV_FILE.bak" 2>/dev/null >>"$1" || true
  chmod 600 "$1"
}
if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] write $ENV_FILE:"; write_env "$TMP/env"; sed 's/^\(FEISHU_WEBHOOK_KEY=\).*/\1<hidden>/' "$TMP/env"
else [ -f "$ENV_FILE" ] && cp -p "$ENV_FILE" "$ENV_FILE.bak"; write_env "$ENV_FILE"; rm -f "$ENV_FILE.bak"; fi

# ---------- service ----------
UNIT_TEXT="[Unit]
Description=Feishu -> Grok Bot webhook bridge ($INSTANCE)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1 HOME=/root
ExecStart=/usr/bin/env python3 $PREFIX/bridge/forwarder.py $ENV_FILE
ExecStopPost=-/bin/sh -c '$LARK_BIN event stop >/dev/null 2>&1 || true'
Restart=always
RestartSec=3
KillMode=control-group
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target"
if [ "$HAS_SYSTEMD" = 1 ]; then
  info "installing systemd unit $UNIT"
  if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] $UNIT:"; echo "$UNIT_TEXT"
  else printf '%s\n' "$UNIT_TEXT" >"$UNIT"; systemctl daemon-reload; systemctl enable "$NAME" >/dev/null 2>&1
    if [ "$NO_START" = 0 ]; then
      systemctl restart "$NAME"
      # code in $PREFIX is shared: restart the other already-running instances so they pick up the new version
      for u in "$UNIT_DIR"/$BASE.service "$UNIT_DIR"/$BASE-*.service; do
        [ -f "$u" ] || continue; s=$(basename "$u" .service); [ "$s" = "$NAME" ] && continue
        systemctl is-active --quiet "$s" && { info "restarting $s (shared code updated)"; systemctl restart "$s" || warn "restart $s failed"; }
      done
      sleep 5
    fi
  fi
else
  warn "systemd not found; using fallback loop ($PREFIX/scripts/start.sh). It won't survive reboot; add to crontab: @reboot $PREFIX/scripts/start.sh"
  [ "$NO_START" = 1 ] || run "$PREFIX/scripts/start.sh" "$ENV_FILE"
fi

# ---------- status ----------
echo
if [ "$DRY_RUN" = 1 ]; then info "dry-run complete, nothing changed"; exit 0; fi
if [ -n "$ROOT" ]; then info "installed into test root $ROOT"; exit 0; fi
if [ "$HAS_SYSTEMD" = 1 ]; then
  systemctl --no-pager --lines=0 status "$NAME" || true
  journalctl -u "$NAME" -n 15 --no-pager 2>/dev/null || true
  systemctl is-active --quiet "$NAME" && info "安装完成，服务运行中 ✅" || warn "服务未处于 active 状态，查看上面的日志"
  cat <<TXT

常用命令：
  服务名         $NAME
  查看实时日志   journalctl -u $NAME -f
  最近日志       journalctl -u $NAME -n 100 --no-pager
  状态/重启/停止 systemctl status|restart|stop $NAME
  事件总线状态   LARKSUITE_CLI_CONFIG_DIR=$LCFG $LARK_BIN event status
  修改配置       编辑 $ENV_FILE 后 systemctl restart $NAME（webhook key 修改无需重启）
  重新安装/升级  再次运行同一条 curl 命令（回车保留旧值）（实例 $INSTANCE）
TXT
else
  info "日志：tail -f $WORK/forwarder.log"
fi
