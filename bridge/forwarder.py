#!/usr/bin/env python3
"""Feishu im.message.receive_v1 -> Grok Bot webhook forwarder.

Wraps `lark-cli event consume im.message.receive_v1 --as bot`; all IDs/secrets come from an env file
(default /etc/feishu-grokbot-bridge.env, override with BRIDGE_ENV_FILE or argv[1]).

Features: owner vs guest roles (guests only in groups when @bot or @all), skip the bot's own messages,
persistent message_id dedupe, skip messages older than STALE_SEC, polling backup of known chats every
POLL_EVERY seconds, on-disk retry queues, and a light bus watchdog (restart if the SDK logged a
disconnect and the bus then stayed silent).
"""
import fcntl, json, logging, os, re, shlex, signal, subprocess, sys, threading, time, urllib.request, uuid
from collections import OrderedDict

DEFAULT_ENV = "/etc/feishu-grokbot-bridge.env"
ALL_KEYS = {"@_all", "all"}
DEFAULTS = {
    "FEISHU_APP_ID": "", "FEISHU_OWNER_OPEN_IDS": "", "FEISHU_BOT_OPEN_ID": "",
    "FEISHU_WEBHOOK_URL": "", "FEISHU_WEBHOOK_KEY": "", "FEISHU_SEED_CHATS": "",
    "BRIDGE_WORK_DIR": "/var/lib/feishu-grokbot-bridge", "LARK_CLI": "lark-cli", "LARK_CLI_EXTRA_ARGS": "",
    "LARKSUITE_CLI_CONFIG_DIR": "", "BRIDGE_LOG_FILE": "",
    "RETRY_EVERY": "60", "SEEN_MAX": "5000", "POLL_EVERY": "90", "POLL_WINDOW": "300", "STALE_SEC": "900",
    "WATCHDOG_EVERY": "15", "WATCHDOG_QUIET_AFTER_DISCONNECT": "60", "WATCHDOG_SILENCE_MAX": "0",
}
log = logging.getLogger("feishu-bridge")


def parse_env_file(path):
    vals = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.replace("export ", "", 1).strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                vals[k] = v
    except FileNotFoundError:
        pass
    return vals


def load_config(env_file, environ=None):
    """Env file overrides process env. Re-read on each POST so webhook key rotation needs no restart."""
    environ = os.environ if environ is None else environ
    c = {k: environ.get(k, d) for k, d in DEFAULTS.items()}
    c.update({k: v for k, v in parse_env_file(env_file).items() if k in DEFAULTS})
    c["owners"] = {x.strip() for x in c["FEISHU_OWNER_OPEN_IDS"].split(",") if x.strip()}
    c["seed_chats"] = [x.strip() for x in c["FEISHU_SEED_CHATS"].split(",") if x.strip().startswith("oc_")]
    for k in ("RETRY_EVERY", "SEEN_MAX", "POLL_EVERY", "POLL_WINDOW", "STALE_SEC", "WATCHDOG_EVERY",
              "WATCHDOG_QUIET_AFTER_DISCONNECT", "WATCHDOG_SILENCE_MAX"):
        c[k] = int(c[k] or DEFAULTS[k])
    return c


def is_mentioned(ev, bot_open_id):
    for m in ev.get("mentions") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if isinstance(mid, dict):
            mid = mid.get("open_id")
        if (bot_open_id and mid == bot_open_id) or m.get("key") in ALL_KEYS or mid in ALL_KEYS:
            return True
    return False


def classify(ev, owners, bot_open_id):
    """Return (role or None, reason)."""
    sender = ev.get("sender_id") or ""
    if bot_open_id and sender == bot_open_id:
        return None, "self"
    if sender in owners:
        return "owner", "owner"
    if ev.get("chat_type") != "group":
        return None, "guest outside group"
    if not is_mentioned(ev, bot_open_id):
        return None, "guest without @bot/@all"
    return "guest", "guest @bot/@all"


def msg_age_sec(ct, now=None):
    try:
        t = int(ct)
        if t > 10 ** 12:
            t //= 1000
        return (now or time.time()) - t
    except Exception:
        return 0


def render_content(mtype, raw):
    try:
        c = json.loads(raw or "{}")
    except Exception:
        return raw or ""
    if mtype == "text":
        return c.get("text", "")
    if mtype == "post":
        body = c if "content" in c else next((v for v in c.values() if isinstance(v, dict) and "content" in v), {})
        parts = [body.get("title", "")] if body.get("title") else []
        for para in body.get("content", []) or []:
            parts.append("".join(e.get("text", "") or (("@" + e.get("user_name", "")) if e.get("tag") == "at" else "")
                                 for e in para if isinstance(e, dict)))
        return "\n".join(p for p in parts if p)
    return raw or ""


def normalize_polled(item):
    snd = item.get("sender") or {}
    return {"message_id": item.get("message_id"), "chat_id": item.get("chat_id"), "chat_type": item.get("chat_type"),
            "sender_id": snd.get("id", ""), "message_type": item.get("msg_type"),
            "content": render_content(item.get("msg_type"), (item.get("body") or {}).get("content")),
            "mentions": [{"id": m.get("id"), "key": m.get("key")} for m in (item.get("mentions") or [])],
            "create_time": item.get("create_time"), "_via": "poll"}


def build_payload(ev, role):
    return {"message_id": ev.get("message_id") or ev.get("id"), "chat_id": ev.get("chat_id"),
            "chat_type": ev.get("chat_type"), "sender_open_id": ev.get("sender_id") or "", "role": role,
            "message_type": ev.get("message_type"), "text": ev.get("content", ""), "create_time": ev.get("create_time")}


class Seen:
    def __init__(self, path, maxn=5000):
        self.path, self.max = path, maxn
        self.d = OrderedDict(); self.lock = threading.Lock()
        try:
            for l in open(path):
                l = l.strip()
                if l:
                    self.d.setdefault(l, 1)
        except FileNotFoundError:
            pass

    def __contains__(self, mid):
        with self.lock:
            return mid in self.d

    def add(self, mid):
        with self.lock:
            if mid in self.d:
                return False
            self.d[mid] = 1
            if len(self.d) > self.max:
                while len(self.d) > self.max:
                    self.d.popitem(last=False)
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    f.write("\n".join(self.d) + "\n")
                os.replace(tmp, self.path)
            else:
                with open(self.path, "a") as f:
                    f.write(mid + "\n")
            return True


def enqueue(d, obj):
    name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    tmp = os.path.join(d, "." + name)
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(d, name))


def queued(d):
    return sorted(x for x in os.listdir(d) if x.endswith(".json") and not x.startswith("."))


class Bridge:
    def __init__(self, env_file):
        self.env_file = env_file
        self.c = load_config(env_file)
        if self.c["LARKSUITE_CLI_CONFIG_DIR"]:
            os.environ["LARKSUITE_CLI_CONFIG_DIR"] = self.c["LARKSUITE_CLI_CONFIG_DIR"]
        self.work = self.c["BRIDGE_WORK_DIR"]
        self.whq = os.path.join(self.work, "outbox-webhook"); self.replyq = os.path.join(self.work, "outbox")
        for d in (self.work, self.whq, self.replyq):
            os.makedirs(d, exist_ok=True)
        self.chats_file = os.path.join(self.work, "chats.txt")
        self.seen = Seen(os.path.join(self.work, "seen_ids.txt"), self.c["SEEN_MAX"])
        self.qlock = threading.Lock(); self.chats_lock = threading.Lock()
        self.lark = [self.c["LARK_CLI"]] + shlex.split(self.c["LARK_CLI_EXTRA_ARGS"])
        self.proc = None

    # ---- webhook ----
    def post(self, payload):
        c = load_config(self.env_file)
        if not c["FEISHU_WEBHOOK_URL"]:
            return False, "FEISHU_WEBHOOK_URL not set"
        req = urllib.request.Request(c["FEISHU_WEBHOOK_URL"], method="POST",
                                     data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {c['FEISHU_WEBHOOK_KEY']}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return 200 <= r.status < 300, f"HTTP {r.status}"
        except Exception as e:
            return False, repr(e)

    # ---- chats (for polling) ----
    def load_chats(self):
        cs = list(self.c["seed_chats"])
        try:
            cs += [l.strip() for l in open(self.chats_file) if l.strip().startswith("oc_")]
        except FileNotFoundError:
            pass
        return list(dict.fromkeys(cs))

    def remember_chat(self, cid):
        if not cid or not cid.startswith("oc_"):
            return
        with self.chats_lock:
            if cid in self.load_chats():
                return
            with open(self.chats_file, "a") as f:
                f.write(cid + "\n")
            log.info("poll: new chat %s", cid)

    # ---- core ----
    def handle(self, ev):
        self.remember_chat(ev.get("chat_id"))
        mid = ev.get("message_id") or ev.get("id")
        if not mid:
            return "no id"
        role, why = classify(ev, self.c["owners"], self.c["FEISHU_BOT_OPEN_ID"])
        if role is None:
            log.info("skip %s from %s: %s", mid, ev.get("sender_id"), why); return "skip"
        if not self.seen.add(mid):
            log.info("dup %s", mid); return "dup"
        age = msg_age_sec(ev.get("create_time"))
        if age > self.c["STALE_SEC"]:
            log.warning("stale skip %s age=%.0fs", mid, age); return "stale"
        payload = build_payload(ev, role)
        with self.qlock:
            backlog = bool(queued(self.whq))
        ok, info = (False, "backlog pending (preserve order)") if backlog else self.post(payload)
        if ok:
            log.info("forwarded %s role=%s (%s)", mid, role, info); return "forwarded"
        enqueue(self.whq, {"payload": payload, "attempts": 0}); log.warning("queued %s: %s", mid, info)
        return "queued"

    def run_lark(self, args, timeout=60):
        return subprocess.run(self.lark + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def retry_loop(self):
        while True:
            try:
                with self.qlock:
                    for f in queued(self.whq):
                        p = os.path.join(self.whq, f); item = json.load(open(p))
                        ok, info = self.post(item["payload"])
                        if ok:
                            os.remove(p); log.info("retry ok %s", item["payload"]["message_id"])
                        else:
                            item["attempts"] = item.get("attempts", 0) + 1; json.dump(item, open(p, "w"), ensure_ascii=False)
                            log.info("retry webhook failed (%s); %s pending", info, f); break
                for f in queued(self.replyq):
                    p = os.path.join(self.replyq, f); item = json.load(open(p))
                    r = self.run_lark(["im", "+messages-reply", "--as", "bot", "--message-id", item["message_id"],
                                       "--text", item["text"], "--idempotency-key", item.get("idem", f[:50])])
                    if r.returncode == 0:
                        os.remove(p); log.info("reply retry ok %s", item["message_id"])
                    else:
                        item["attempts"] = item.get("attempts", 0) + 1; json.dump(item, open(p, "w"), ensure_ascii=False)
                        log.warning("reply retry failed %s: %s", item["message_id"], (r.stderr or r.stdout)[-300:]); break
            except Exception as e:
                log.exception("retry loop error: %s", e)
            time.sleep(self.c["RETRY_EVERY"])

    def poll_loop(self):
        if self.c["POLL_EVERY"] <= 0:
            return
        done = OrderedDict(); errs = {}
        time.sleep(30)
        while True:
            for cid in self.load_chats():
                try:
                    params = {"container_id_type": "chat", "container_id": cid,
                              "start_time": str(int(time.time()) - self.c["POLL_WINDOW"]),
                              "sort_type": "ByCreateTimeAsc", "page_size": "50"}
                    r = self.run_lark(["api", "GET", "/open-apis/im/v1/messages", "--as", "bot", "--params", json.dumps(params)])
                    d = None
                    for out in (r.stdout, r.stderr):
                        try:
                            d = json.loads(out); break
                        except Exception:
                            pass
                    if d is None:
                        txt = (r.stdout or "") + (r.stderr or "")
                        m = re.search(r'"code":\s*(\d+)', txt); mm = re.search(r'"message":\s*"([^"]*)"', txt)
                        d = {"ok": False, "error": {"code": m and int(m.group(1)), "message": mm.group(1) if mm else txt.strip()[-150:]}}
                    if not d.get("ok"):
                        e = d.get("error") or {}; key = (e.get("code"), e.get("message"))
                        if errs.get(cid) != key:
                            log.warning("poll %s failed: code=%s %s", cid, e.get("code"), e.get("message")); errs[cid] = key
                        continue
                    if cid in errs:
                        log.info("poll %s recovered", cid); errs.pop(cid)
                    for it in (d.get("data") or {}).get("items") or []:
                        mid = it.get("message_id")
                        if not mid or mid in done:
                            continue
                        done[mid] = 1
                        while len(done) > 2000:
                            done.popitem(last=False)
                        if (it.get("sender") or {}).get("sender_type") == "app" or it.get("deleted") or mid in self.seen:
                            continue
                        log.info("poll: unseen msg %s in %s", mid, cid)
                        self.handle(normalize_polled(it))
                except Exception as e:
                    log.exception("poll %s error: %s", cid, e)
            time.sleep(self.c["POLL_EVERY"])

    # ---- light watchdog: SDK reconnect normally recovers on a direct network; only act if it doesn't ----
    def buslog_path(self):
        base = os.environ.get("LARKSUITE_CLI_CONFIG_DIR") or os.path.expanduser("~/.lark-cli")
        return os.path.join(base, "events", re.sub(r"[^A-Za-z0-9_.-]", "_", self.c["FEISHU_APP_ID"]), "bus.log")

    def watchdog_loop(self):
        path = self.buslog_path(); started = time.time()
        pos = os.path.getsize(path) if os.path.exists(path) else 0
        disconnect_at = None
        while True:
            time.sleep(self.c["WATCHDOG_EVERY"])
            try:
                if not os.path.exists(path):
                    continue
                size = os.path.getsize(path)
                if size < pos:
                    pos = 0
                new = ""
                if size > pos:
                    with open(path, errors="replace") as f:
                        f.seek(pos); new = f.read(); pos = f.tell()
                now = time.time()
                lines = [l for l in new.splitlines() if l.strip()]
                bad = [l for l in lines if re.search(r"\[SDK\] (disconnected|trying to reconnect)|\[SDK ERROR\]", l)]
                if bad:
                    disconnect_at = now; log.warning("watchdog: SDK disconnect seen: %s", bad[-1][-200:])
                elif lines and disconnect_at:
                    log.info("watchdog: bus active again after disconnect, SDK reconnect assumed OK"); disconnect_at = None
                if disconnect_at and now - disconnect_at >= self.c["WATCHDOG_QUIET_AFTER_DISCONNECT"]:
                    self.restart("disconnect seen and no bus activity for %ds" % (now - disconnect_at))
                smax = self.c["WATCHDOG_SILENCE_MAX"]
                if smax > 0 and now - os.path.getmtime(path) >= smax and now - started >= smax:
                    self.restart("bus silent for %ds" % (now - os.path.getmtime(path)))
            except Exception as e:
                log.exception("watchdog error: %s", e)

    def restart(self, why):
        log.warning("watchdog restart: %s", why)
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            self.run_lark(["event", "stop"], timeout=20)
        except Exception:
            pass
        logging.shutdown()
        os._exit(75)  # systemd Restart=always brings us back with a fresh bus

    def main(self):
        lockf = open(os.path.join(self.work, "forwarder.lock"), "w")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another forwarder is running", file=sys.stderr); sys.exit(1)
        with open(os.path.join(self.work, "forwarder.pid"), "w") as f:
            f.write(str(os.getpid()))
        missing = [k for k in ("FEISHU_APP_ID", "FEISHU_WEBHOOK_URL") if not self.c[k]]
        if not self.c["owners"]:
            missing.append("FEISHU_OWNER_OPEN_IDS")
        if missing:
            log.error("missing config: %s (env file %s)", ", ".join(missing), self.env_file); sys.exit(2)
        signal.signal(signal.SIGTERM, lambda *a: self.restart_quiet())
        for fn in (self.retry_loop, self.poll_loop, self.watchdog_loop):
            threading.Thread(target=fn, daemon=True).start()
        backoff = 5
        while True:
            log.info("starting lark-cli event consume (app %s)", self.c["FEISHU_APP_ID"])
            start = time.time()
            self.proc = p = subprocess.Popen(self.lark + ["event", "consume", "im.message.receive_v1", "--as", "bot"],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             universal_newlines=True, bufsize=1)
            threading.Thread(target=lambda: [log.info("[lark] %s", l.rstrip()) for l in p.stderr], daemon=True).start()
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.handle(json.loads(line))
                except Exception as e:
                    log.exception("bad event %r: %s", line[:300], e)
            rc = p.wait(); log.warning("consume exited rc=%s", rc)
            backoff = 5 if time.time() - start > 300 else min(backoff * 2, 300)
            time.sleep(backoff)

    def restart_quiet(self):
        log.info("SIGTERM, stopping")
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        logging.shutdown(); os._exit(0)


def setup_logging(c):
    fmt = "%(asctime)s %(levelname)s %(message)s"
    if c["BRIDGE_LOG_FILE"]:
        logging.basicConfig(filename=c["BRIDGE_LOG_FILE"], level=logging.INFO, format=fmt)
    else:  # stdout -> journald under systemd
        logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(message)s")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    env_file = argv[0] if argv else os.environ.get("BRIDGE_ENV_FILE", DEFAULT_ENV)
    b = Bridge(env_file)
    setup_logging(b.c)
    b.main()


if __name__ == "__main__":
    main()
