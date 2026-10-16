#!/usr/bin/env python3
"""Feishu im.message.receive_v1 -> Grok Bot webhook forwarder.

Wraps `lark-cli event consume im.message.receive_v1 --as bot`, filters events,
dedupes by message_id and POSTs a compact JSON payload to a webhook.
Failed deliveries are queued on disk and retried every RETRY_EVERY seconds.
"""
import fcntl, json, logging, os, shlex, subprocess, sys, threading, time, urllib.request, uuid
from collections import OrderedDict

log = logging.getLogger("feishu-bridge")
CONFIG_KEYS = ("FEISHU_OWNER_OPEN_IDS", "FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK_KEY", "FEISHU_BOT_OPEN_ID",
               "BRIDGE_WORK_DIR", "LARK_CLI", "LARK_CLI_EXTRA_ARGS", "RETRY_EVERY", "SEEN_MAX")
ALL_MENTION_KEYS = {"@_all", "all"}


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
                vals[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return vals


def load_config(env_file=None, environ=None):
    """Env file values override process env (re-read on each POST so key rotation needs no restart)."""
    environ = os.environ if environ is None else environ
    cfg = {k: environ.get(k, "") for k in CONFIG_KEYS}
    if env_file:
        cfg.update({k: v for k, v in parse_env_file(env_file).items() if k in CONFIG_KEYS})
    cfg["owners"] = {x.strip() for x in cfg["FEISHU_OWNER_OPEN_IDS"].split(",") if x.strip()}
    return cfg


def is_mentioned(ev, bot_open_id):
    """True if a group message @-mentions this bot or @all."""
    for m in ev.get("mentions") or []:
        mid = m.get("id")
        if isinstance(mid, dict):  # tolerate raw OpenAPI shape {"open_id": ...}
            mid = mid.get("open_id")
        if bot_open_id and mid == bot_open_id:
            return True
        if m.get("key") in ALL_MENTION_KEYS or mid in ALL_MENTION_KEYS:
            return True
    content = ev.get("content") or ""
    return "@_all" in content or '<at user_id="all">' in content


def should_forward(ev, owners, bot_open_id):
    """Return (ok, reason)."""
    sender = ev.get("sender_id") or ""
    if not (ev.get("message_id") or ev.get("id")):
        return False, "no message_id"
    if sender not in owners:
        return False, "sender not owner"
    if bot_open_id and sender == bot_open_id:
        return False, "own message"
    if ev.get("chat_type") == "group" and not is_mentioned(ev, bot_open_id):
        return False, "group message without @bot/@all"
    return True, "ok"


def build_payload(ev):
    return {"message_id": ev.get("message_id") or ev.get("id"), "chat_id": ev.get("chat_id"),
            "chat_type": ev.get("chat_type"), "sender_open_id": ev.get("sender_id"),
            "message_type": ev.get("message_type"), "text": ev.get("content", ""),
            "create_time": ev.get("create_time")}


class Seen:
    """Persistent bounded LRU-ish set of message_ids."""
    def __init__(self, path, maxlen=5000):
        self.path, self.maxlen = path, maxlen
        self.d = OrderedDict(); self.lock = threading.Lock()
        try:
            with open(path) as f:
                for l in f:
                    l = l.strip()
                    if l:
                        self.d[l] = 1
        except FileNotFoundError:
            pass

    def add(self, mid):
        with self.lock:
            if mid in self.d:
                return False
            self.d[mid] = 1
            if len(self.d) > self.maxlen:
                while len(self.d) > self.maxlen:
                    self.d.popitem(last=False)
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    f.write("\n".join(self.d) + "\n")
                os.replace(tmp, self.path)
            else:
                with open(self.path, "a") as f:
                    f.write(mid + "\n")
            return True


class Queue:
    """Directory-backed FIFO; files are written atomically (tmp + rename)."""
    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)

    def items(self):
        return sorted(x for x in os.listdir(self.path) if x.endswith(".json") and not x.startswith("."))

    def __len__(self):
        return len(self.items())

    def put(self, obj):
        # ns timestamp + per-process counter keeps FIFO order even within one millisecond
        Queue._seq = getattr(Queue, "_seq", 0) + 1
        name = f"{time.time_ns():020d}-{Queue._seq:06d}-{uuid.uuid4().hex[:6]}.json"
        tmp = os.path.join(self.path, "." + name)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(self.path, name))
        return name

    def drain(self, send):
        """Send items in order; stop at first failure (preserves order). Returns number sent."""
        n = 0
        for name in self.items():
            p = os.path.join(self.path, name)
            with open(p, encoding="utf-8") as f:
                item = json.load(f)
            ok, info = send(item)
            if ok:
                os.remove(p); n += 1
            else:
                item["attempts"] = item.get("attempts", 0) + 1
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(item, f, ensure_ascii=False)
                log.info("retry failed (%s); %s pending", info, name)
                break
        return n


class Bridge:
    def __init__(self, work_dir, env_file=None, poster=None):
        self.work_dir, self.env_file = work_dir, env_file
        self.cfg = load_config(env_file)
        self.seen = Seen(os.path.join(work_dir, "seen_ids.txt"), int(self.cfg["SEEN_MAX"] or 5000))
        self.wh_q = Queue(os.path.join(work_dir, "outbox-webhook"))
        self.reply_q = Queue(os.path.join(work_dir, "outbox"))
        self.qlock = threading.Lock()
        self.poster = poster or self.post

    def post(self, payload):
        cfg = load_config(self.env_file)
        if not cfg["FEISHU_WEBHOOK_URL"]:
            return False, "FEISHU_WEBHOOK_URL not set"
        req = urllib.request.Request(cfg["FEISHU_WEBHOOK_URL"], method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['FEISHU_WEBHOOK_KEY']}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return 200 <= r.status < 300, f"HTTP {r.status}"
        except Exception as e:
            return False, repr(e)

    def handle(self, ev):
        ok, why = should_forward(ev, self.cfg["owners"], self.cfg["FEISHU_BOT_OPEN_ID"])
        mid = ev.get("message_id") or ev.get("id")
        if not ok:
            log.info("skip %s: %s", mid, why); return "skip"
        if not self.seen.add(mid):
            log.info("dup %s", mid); return "dup"
        payload = build_payload(ev)
        with self.qlock:
            backlog = len(self.wh_q) > 0
        ok, info = (False, "backlog pending (preserve order)") if backlog else self.poster(payload)
        if ok:
            log.info("forwarded %s (%s)", mid, info); return "sent"
        self.wh_q.put({"payload": payload, "attempts": 0})
        log.warning("queued %s: %s", mid, info); return "queued"

    def retry_once(self):
        with self.qlock:
            self.wh_q.drain(lambda item: self.poster(item["payload"]))
        self.reply_q.drain(self._send_reply)

    def _send_reply(self, item):
        r = subprocess.run(self.lark_cmd("im", "+messages-reply", "--as", "bot", "--message-id", item["message_id"],
                           "--text", item["text"], "--idempotency-key", item.get("idem", item["message_id"])[:50]),
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return r.returncode == 0, (r.stderr or r.stdout)[-300:]

    def lark_cmd(self, *args):
        lark = self.cfg["LARK_CLI"] or "lark-cli"
        return [lark, *shlex.split(self.cfg["LARK_CLI_EXTRA_ARGS"] or ""), *args]

    def retry_loop(self):
        every = int(self.cfg["RETRY_EVERY"] or 60)
        while True:
            try:
                self.retry_once()
            except Exception as e:
                log.exception("retry loop error: %s", e)
            time.sleep(every)

    def run(self):
        lockf = open(os.path.join(self.work_dir, "forwarder.lock"), "w")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another forwarder is running in this work dir", file=sys.stderr); sys.exit(1)
        with open(os.path.join(self.work_dir, "forwarder.pid"), "w") as f:
            f.write(str(os.getpid()))
        if not self.cfg["owners"]:
            log.warning("FEISHU_OWNER_OPEN_IDS empty: every message will be skipped")
        threading.Thread(target=self.retry_loop, daemon=True).start()
        backoff = 5
        while True:
            log.info("starting lark-cli event consume")
            start = time.time()
            p = subprocess.Popen(self.lark_cmd("event", "consume", "im.message.receive_v1", "--as", "bot"),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, bufsize=1)
            threading.Thread(target=lambda: [log.info("[lark] %s", l.rstrip()) for l in p.stderr], daemon=True).start()
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.handle(json.loads(line))
                except Exception as e:
                    log.exception("bad event %r: %s", line[:300], e)
            log.warning("consume exited rc=%s", p.wait())
            backoff = 5 if time.time() - start > 300 else min(backoff * 2, 300)
            time.sleep(backoff)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env-file", default=os.environ.get("BRIDGE_ENV_FILE", ".env"))
    ap.add_argument("--work-dir", default=None, help="instance dir (state, queues, logs); default BRIDGE_WORK_DIR or ./run")
    a = ap.parse_args(argv)
    cfg = load_config(a.env_file)
    work = os.path.abspath(a.work_dir or cfg["BRIDGE_WORK_DIR"] or "run")
    os.makedirs(work, exist_ok=True)
    logging.basicConfig(filename=os.path.join(work, "forwarder.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    Bridge(work, a.env_file).run()


if __name__ == "__main__":
    main()
