import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge import forwarder as fw

OWNER, BOT, GUEST = "ou_owner", "ou_bot", "ou_guest"


def ev(**kw):
    e = {"message_id": "om_1", "chat_id": "oc_1", "chat_type": "p2p", "sender_id": OWNER,
         "message_type": "text", "content": "hi", "mentions": [], "create_time": str(int(time.time() * 1000))}
    e.update(kw); return e


def test_classify():
    o = {OWNER}
    assert fw.classify(ev(), o, BOT)[0] == "owner"
    assert fw.classify(ev(sender_id=BOT), o, BOT)[0] is None
    assert fw.classify(ev(sender_id=GUEST), o, BOT)[0] is None
    assert fw.classify(ev(sender_id=GUEST, chat_type="group"), o, BOT)[0] is None
    assert fw.classify(ev(sender_id=GUEST, chat_type="group", mentions=[{"id": BOT, "key": "@_user_1"}]), o, BOT)[0] == "guest"
    assert fw.classify(ev(sender_id=GUEST, chat_type="group", mentions=[{"id": "x", "key": "@_all"}]), o, BOT)[0] == "guest"


def test_config(tmp_path):
    p = tmp_path / "e.env"
    p.write_text('FEISHU_OWNER_OPEN_IDS="a, b"\nexport POLL_EVERY=10\nFEISHU_WEBHOOK_KEY=\'k=1\'\n')
    c = fw.load_config(str(p), environ={})
    assert c["owners"] == {"a", "b"} and c["POLL_EVERY"] == 10 and c["FEISHU_WEBHOOK_KEY"] == "k=1"


def make(tmp_path, monkeypatch, posts):
    p = tmp_path / "e.env"
    p.write_text(f"FEISHU_APP_ID=cli_x\nFEISHU_OWNER_OPEN_IDS={OWNER}\nFEISHU_BOT_OPEN_ID={BOT}\n"
                 f"FEISHU_WEBHOOK_URL=http://x\nFEISHU_WEBHOOK_KEY=k\nBRIDGE_WORK_DIR={tmp_path}/w\n")
    b = fw.Bridge(str(p))
    monkeypatch.setattr(b, "post", lambda pl: (posts.append(pl) or True, "HTTP 200"))
    return b


def test_handle_dedupe_stale(tmp_path, monkeypatch):
    posts = []; b = make(tmp_path, monkeypatch, posts)
    assert b.handle(ev()) == "forwarded"
    assert b.handle(ev()) == "dup"
    assert b.handle(ev(message_id="om_2", create_time=str(int(time.time() - 3600) * 1000))) == "stale"
    assert posts[0]["role"] == "owner" and posts[0]["text"] == "hi"
    assert open(b.chats_file).read().strip() == "oc_1"


def test_queue_on_failure(tmp_path, monkeypatch):
    b = make(tmp_path, monkeypatch, [])
    monkeypatch.setattr(b, "post", lambda pl: (False, "down"))
    assert b.handle(ev()) == "queued" and len(fw.queued(b.whq)) == 1


def test_render_post():
    raw = json.dumps({"zh_cn": {"title": "T", "content": [[{"tag": "text", "text": "a"}, {"tag": "at", "user_name": "B"}]]}})
    assert fw.render_content("post", raw) == "T\na@B"
