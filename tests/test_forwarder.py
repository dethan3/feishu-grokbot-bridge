import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from bridge.forwarder import Bridge, Queue, Seen, build_payload, is_mentioned, load_config, should_forward

OWNER, OTHER, BOT = "ou_owner_example", "ou_someone_else", "ou_bot_example"


def ev(**kw):
    base = {"message_id": "om_1", "chat_id": "oc_1", "chat_type": "p2p", "sender_id": OWNER,
            "message_type": "text", "content": "hi", "create_time": "1700000000000"}
    base.update(kw); return base


class TestFilter:
    def test_p2p_owner(self): assert should_forward(ev(), {OWNER}, BOT)[0]
    def test_non_owner(self): assert not should_forward(ev(sender_id=OTHER), {OWNER}, BOT)[0]
    def test_multiple_owners(self): assert should_forward(ev(sender_id=OTHER), {OWNER, OTHER}, BOT)[0]
    def test_no_owners_configured(self): assert not should_forward(ev(), set(), BOT)[0]
    def test_missing_id(self): assert not should_forward(ev(message_id=None), {OWNER}, BOT)[0]
    def test_legacy_id(self): assert should_forward(ev(message_id=None, id="om_x"), {OWNER}, BOT)[0]
    def test_group_without_mention(self): assert not should_forward(ev(chat_type="group"), {OWNER}, BOT)[0]
    def test_group_at_bot(self):
        e = ev(chat_type="group", mentions=[{"id": BOT, "key": "@_user_1", "name": "bot"}])
        assert should_forward(e, {OWNER}, BOT)[0]
    def test_group_at_other_bot(self):
        e = ev(chat_type="group", mentions=[{"id": "ou_other_bot", "key": "@_user_1"}])
        assert not should_forward(e, {OWNER}, BOT)[0]
    def test_group_at_all(self):
        assert should_forward(ev(chat_type="group", mentions=[{"key": "@_all"}]), {OWNER}, BOT)[0]
        assert should_forward(ev(chat_type="group", content="@_all hello"), {OWNER}, BOT)[0]
    def test_raw_mention_shape(self):
        assert is_mentioned({"mentions": [{"id": {"open_id": BOT}}]}, BOT)
    def test_no_bot_id_group_requires_all(self):
        e = ev(chat_type="group", mentions=[{"id": BOT}])
        assert not should_forward(e, {OWNER}, "")[0]


def test_config(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\nexport FEISHU_OWNER_OPEN_IDS="ou_a, ou_b,,"\nFEISHU_WEBHOOK_KEY=\'k\'\nIGNORED=1\n')
    c = load_config(str(f), environ={"FEISHU_WEBHOOK_URL": "https://example.invalid/hook"})
    assert c["owners"] == {"ou_a", "ou_b"} and c["FEISHU_WEBHOOK_KEY"] == "k"
    assert c["FEISHU_WEBHOOK_URL"] == "https://example.invalid/hook" and "IGNORED" not in c


def test_payload_shape():
    assert set(build_payload(ev())) == {"message_id", "chat_id", "chat_type", "sender_open_id",
                                        "message_type", "text", "create_time"}


class TestSeen:
    def test_dedupe_persists(self, tmp_path):
        p = str(tmp_path / "seen")
        s = Seen(p); assert s.add("a") and not s.add("a")
        assert not Seen(p).add("a")
    def test_bounded(self, tmp_path):
        p = str(tmp_path / "seen"); s = Seen(p, maxlen=3)
        for i in range(5): s.add(str(i))
        assert list(s.d) == ["2", "3", "4"]
        assert open(p).read().split() == ["2", "3", "4"]
        assert Seen(p, 3).add("0")


class TestQueue:
    def test_fifo_and_stop_on_failure(self, tmp_path):
        q = Queue(str(tmp_path / "q"))
        for i in range(3): q.put({"n": i})
        sent = []
        def send(item):
            if item["n"] == 1: return False, "boom"
            sent.append(item["n"]); return True, "ok"
        assert q.drain(send) == 1 and sent == [0] and len(q) == 2
        first = json.load(open(os.path.join(q.path, q.items()[0])))
        assert first == {"n": 1, "attempts": 1}
        assert q.drain(lambda i: (True, "")) == 2 and len(q) == 0
    def test_ignores_tmp_files(self, tmp_path):
        q = Queue(str(tmp_path)); open(tmp_path / ".partial.json", "w").write("{")
        assert len(q) == 0


class TestBridge:
    def make(self, tmp_path, results):
        env = tmp_path / ".env"
        env.write_text(f"FEISHU_OWNER_OPEN_IDS={OWNER}\nFEISHU_BOT_OPEN_ID={BOT}\n")
        calls = []
        def poster(p):
            calls.append(p["message_id"]); return results.pop(0) if results else (True, "ok")
        return Bridge(str(tmp_path), str(env), poster=poster), calls

    def test_send_dup_skip(self, tmp_path):
        b, calls = self.make(tmp_path, [])
        assert b.handle(ev()) == "sent"
        assert b.handle(ev()) == "dup"
        assert b.handle(ev(message_id="om_2", sender_id=OTHER)) == "skip"
        assert calls == ["om_1"]

    def test_queue_preserves_order(self, tmp_path):
        b, calls = self.make(tmp_path, [(False, "down")])
        assert b.handle(ev(message_id="om_1")) == "queued"
        assert b.handle(ev(message_id="om_2")) == "queued"  # backlog -> not posted directly
        assert calls == ["om_1"]
        b.wh_q.drain(lambda it: b.poster(it["payload"]))
        assert calls == ["om_1", "om_1", "om_2"] and len(b.wh_q) == 0
