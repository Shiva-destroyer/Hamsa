"""conversation layer -- every journey x en/hi/kn, consent gate, photo path, disputes, caps, idle reset, concurrency,
unsupported types, Graph error decoding, retry, log redaction. Needs the seeded DB (DATABASE_URL), e.g. a scratch database.
wa._post is patched per test (monkeypatch) so this file does not disturb test_webhook_flow.py's module-level capture."""
import io
import json
import os
import threading
import uuid

import httpx
import pytest
from PIL import Image

import config, db, engine, flow, log, snapshot, storage, wa
from texts import t, VERDICT

LANGS = ["en", "hi", "kn"]
PACKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "packs")
SENDERS = []


@pytest.fixture
def out(monkeypatch, tmp_path):
    box = []
    monkeypatch.setattr(wa, "_post", lambda body: box.append(body) or {})
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    yield box
    for s in SENDERS:
        ph = wa.identity_hash(s)
        for sql in ("DELETE FROM reports WHERE reporter_identity_hash=%s", "DELETE FROM disputes WHERE submitter_identity_hash=%s",
                    "DELETE FROM uploads WHERE identity_hash=%s", "DELETE FROM wa_sessions WHERE phone_hash=%s",
                    "DELETE FROM consent_log WHERE user_identifier_hash=%s"):
            db.run(sql, (ph,))
    SENDERS.clear()
    db.run("UPDATE batches SET dispute_status='none' WHERE batch_number='AX2291'")


class Chat:
    def __init__(self, out):
        self.sender = "9198" + uuid.uuid4().hex[:8].translate(str.maketrans("abcdef", "123456")); SENDERS.append(self.sender)
        self.ph, self.out = wa.identity_hash(self.sender), out

    def send(self, kind="text", body="", **kw):
        self.out.clear()
        flow.handle(wa.Inbound("wamid." + uuid.uuid4().hex, self.sender, kind, body, **kw))
        return self

    def text(self, s): return self.send("text", s)
    def button(self, i): return self.send("button", i)
    def cooldown_off(self): db.run("UPDATE wa_sessions SET last_lookup_at=NULL WHERE phone_hash=%s", (self.ph,)); return self

    def onboard(self, lang="en"):
        self.text("hi"); self.button("agree"); self.button("lang_" + lang); return self

    @property
    def said(self):
        return "\n".join(o["text"]["body"] if o["type"] == "text" else o["interactive"]["body"]["text"] for o in self.out if o["type"] in ("text", "interactive"))

    @property
    def types(self): return [o["type"] for o in self.out]

    def session(self): return db.one("SELECT * FROM wa_sessions WHERE phone_hash=%s", (self.ph,))


def png(color=(200, 30, 30)):
    b = io.BytesIO(); Image.new("RGB", (64, 48), color).save(b, "PNG"); return b.getvalue()


# ---------------- journeys x languages ----------------
@pytest.mark.parametrize("lang", LANGS)
def test_journey_verdict_voice_buttons_why_timeline_help(out, lang):
    c = Chat(out).onboard(lang)
    assert c.said == t("menu", lang)
    c.text("AX2291")
    assert c.types == ["text", "audio", "interactive"]                                   # <=3 messages, in order
    assert VERDICT["RED_nsq"]["head"][lang] in c.said and "CDSCO" in c.said
    assert out[1]["audio"]["voice"] is True and out[1]["audio"]["id"].endswith(f"RED_nsq_{lang}.ogg")
    assert [b["reply"]["id"] for b in out[2]["interactive"]["action"]["buttons"]] == ["report", "again", "dispute"]
    c.text("WHY");                 assert "Provenance" in c.said
    c.text("TIMELINE AX2291");     assert "AX2291" in c.said and "NSQ" in c.said
    c.text("HELP");                assert c.said == t("help", lang)
    c.text("TIMELINE");            assert "AX2291" in c.said                              # defaults to the last batch
    c.text("TIMELINE !!");         assert c.said == t("timeline_usage", lang)
    c.text("gibberish words");     assert c.said == t("unknown", lang)


@pytest.mark.parametrize("lang", LANGS)
def test_report_journey_with_photo(out, lang, monkeypatch):
    monkeypatch.setattr(wa, "download_media", lambda mid: png())
    c = Chat(out).onboard(lang); c.text("AX2291")
    c.button("report");  c.button("rt1")
    assert c.said == t("report_desc", lang)
    c.send("image", media_id="m1", mime="image/png")
    assert c.said == t("report_ok", lang)
    r = db.one("SELECT * FROM reports WHERE reporter_identity_hash=%s", (c.ph,))
    assert r["visibility"] == "regulator_only" and r["status"] == "open" and r["image_reference"]
    u = db.one("SELECT * FROM uploads WHERE id=%s", (r["image_reference"],))
    assert u["attached_to_type"] == "report" and str(u["attached_to_id"]) == str(r["id"])
    assert b"Exif" not in open(storage.upload_dir() / u["path"], "rb").read()


def test_report_rejects_bad_media_and_keeps_state(out, monkeypatch):
    monkeypatch.setattr(wa, "download_media", lambda mid: b"not an image at all")
    c = Chat(out).onboard(); c.text("AX2291"); c.button("report"); c.button("rt2")
    c.send("image", media_id="m1", mime="image/png")
    assert c.said == t("media_bad", "en") and c.session()["state"] == "REPORT_DESC"
    c.text("SKIP");  assert c.said == t("report_ok", "en")


@pytest.mark.parametrize("lang", LANGS)
def test_delete_journey(out, lang):
    c = Chat(out).onboard(lang)
    c.text("DELETE");  assert c.said == t("delete_confirm", lang)
    c.text("YES");     assert c.said == t("delete_done", lang)
    assert c.session() is None
    assert db.one("SELECT count(*) n FROM consent_log WHERE user_identifier_hash=%s", (c.ph,))["n"] == 0


def test_delete_declined_by_other_reply_keeps_data(out):
    c = Chat(out).onboard(); c.text("DELETE MY DATA"); c.text("no")
    assert c.session() is not None and c.session()["state"] == "MENU"


@pytest.mark.parametrize("lang", LANGS)
def test_language_switch_and_expiry_flow(out, lang):
    c = Chat(out).onboard("en")
    c.text("LANG");  assert c.types == ["interactive"]
    c.button("lang_" + lang);  assert c.said == t("menu", lang)
    c.text("ZZ9999X");  assert c.said == t("ask_expiry", lang, b="ZZ9999X") and c.session()["state"] == "AWAIT_EXPIRY"
    c.text("13/2028");  assert c.said == t("ask_expiry", lang, b="ZZ9999X")               # invalid month -> re-ask
    c.text("SKIP");     assert c.types == ["text", "audio", "interactive"]


# ---------------- consent gate ----------------
@pytest.mark.parametrize("kind,body,extra", [("text", "AX2291", {}), ("image", "", {"media_id": "m1", "mime": "image/png"}), ("audio", "", {}),
                                             ("button", "report", {}), ("text", "DELETE", {}), ("text", "TIMELINE AX2291", {})])
def test_consent_gate_blocks_everything(out, monkeypatch, kind, body, extra):
    def boom(_): raise AssertionError("media downloaded before consent")
    monkeypatch.setattr(wa, "download_media", boom)
    before = db.one("SELECT count(*) n FROM scan_events WHERE source='whatsapp'")["n"]
    c = Chat(out).send(kind, body, **extra)
    assert "Do you agree" in c.said and [b["reply"]["id"] for b in out[0]["interactive"]["action"]["buttons"]] == ["agree", "decline"]
    assert db.one("SELECT count(*) n FROM scan_events WHERE source='whatsapp'")["n"] == before
    assert c.session()["consented_at"] is None


def test_decline_deletes_session(out):
    c = Chat(out); c.text("hi"); c.button("decline")
    assert c.said == t("declined", "en") and c.session() is None


# ---------------- photo path ----------------
@pytest.mark.parametrize("pack,expected", [("AX2291", "RED"), ("GH6625", "AMBER"), ("MQ7756", "GREEN")])
def test_photo_path_verdicts(out, monkeypatch, pack, expected):
    data = open(os.path.join(PACKS, pack + ".png"), "rb").read()
    monkeypatch.setattr(wa, "download_media", lambda mid: data)
    c = Chat(out).onboard(); c.send("image", media_id="m1", mime="image/png")
    assert c.types == ["text", "audio", "interactive", "text"] and expected in c.said       # verdict, voice, buttons, then "text I read"
    last = c.out[-1]["text"]["body"]
    assert last.startswith(t("read_text", "en", rows="")[:12]) and f"{t('f_batch', 'en')}: {pack}" in last and "(01)" not in last     # required fields only, no raw dump


def test_photo_without_code_but_with_text_returns_text_then_manual_entry_hint(out, monkeypatch):
    from PIL import ImageDraw
    from test_vision import _font
    im = Image.new("RGB", (900, 400), "white")
    ImageDraw.Draw(im).text((40, 150), "Batch No.: ZZ9911", fill="black", font=_font(60))
    b = io.BytesIO(); im.save(b, "PNG"); data = b.getvalue()
    monkeypatch.setattr(wa, "download_media", lambda mid: data)
    c = Chat(out).onboard(); c.send("image", media_id="m1", mime="image/png")
    assert c.types == ["text", "text"] and "ZZ9911" in c.out[0]["text"]["body"] and c.out[1]["text"]["body"] == t("qr_fail", "en")


def test_extraction_failure_never_blocks_the_verdict(out, monkeypatch):
    import extract
    data = open(os.path.join(PACKS, "AX2291.png"), "rb").read()
    monkeypatch.setattr(wa, "download_media", lambda mid: data)
    def boom(raw): raise RuntimeError("tesseract exploded")
    monkeypatch.setattr(extract, "extract_text", boom)
    c = Chat(out).onboard(); c.send("image", media_id="m1", mime="image/png")
    assert c.types == ["text", "audio", "interactive"] and "RED" in c.said


def test_photo_unreadable_and_download_failure_offer_manual_entry(out, monkeypatch):
    c = Chat(out).onboard()
    monkeypatch.setattr(wa, "download_media", lambda mid: png((255, 255, 255)))
    c.send("image", media_id="m1", mime="image/png");  assert c.said == t("qr_fail", "en")
    def bad(mid): raise RuntimeError("media lookup failed")
    monkeypatch.setattr(wa, "download_media", bad)
    c.cooldown_off().send("image", media_id="m2", mime="image/png");  assert c.said == t("qr_fail", "en")


# ---------------- disputes ----------------
@pytest.mark.parametrize("lang", LANGS)
def test_dispute_wrong_then_right_domain_with_document(out, lang, monkeypatch):
    monkeypatch.setattr(wa, "download_media", lambda mid: png())
    c = Chat(out).onboard(lang); c.text("AX2291")
    c.button("dispute");  assert c.said == t("dispute_email", lang, b="AX2291")
    c.text("regulatory@nirmalabio.com");  assert c.said == t("dispute_bad", lang)         # another company's domain
    c.button("dispute"); c.text("qa@amruthadrugs.com");  assert c.said == t("dispute_evid", lang)
    c.send("document", media_id="d1", mime="image/png");  assert c.said == t("dispute_ok", lang, b="AX2291")
    d = db.one("SELECT * FROM disputes WHERE submitter_identity_hash=%s", (c.ph,))
    assert d["channel"] == "whatsapp" and d["submitter_domain"] == "amruthadrugs.com" and d["evidence_reference"].startswith("upload:")
    assert db.one("SELECT attached_to_type t FROM uploads WHERE id=%s", (d["evidence_reference"][7:],))["t"] == "dispute"
    assert db.one("SELECT dispute_status s FROM batches WHERE batch_number='AX2291'")["s"] == "open"
    c.cooldown_off().text("AX2291");  assert VERDICT["RED_nsq"]["head"][lang] in c.said and t("dispute_banner", lang) in c.said


def test_dispute_daily_cap_per_domain(out):
    db.run("""INSERT INTO disputes (id, batch_number, submitted_by, submitter_domain, status, submitted_at, submitter_identity_hash, channel)
              SELECT gen_random_uuid(), 'AX2291', 'x', 'amruthadrugs.com', 'open', now(), %s, 'whatsapp' FROM generate_series(1, 10)""", ("cap-test",))
    try:
        c = Chat(out).onboard(); c.text("AX2291"); c.button("dispute"); c.text("qa@amruthadrugs.com")
        assert c.said == t("dispute_cap", "en") and c.session()["state"] == "MENU"
    finally:
        db.run("DELETE FROM disputes WHERE submitter_identity_hash='cap-test'")


def test_report_daily_cap(out):
    c = Chat(out).onboard(); c.text("AX2291"); sid = c.session()["context"]["last_scan_id"]
    for _ in range(config.DAILY_REPORT_CAP):
        db.run("""INSERT INTO reports (id, scan_id, report_type, reporter_identity_hash, created_at) VALUES (gen_random_uuid(), %s, 'other', %s, now())""", (sid, c.ph))
    c.button("report"); c.button("rt3"); c.text("one too many")
    assert c.said == t("report_cap", "en")
    assert db.one("SELECT count(*) n FROM reports WHERE reporter_identity_hash=%s", (c.ph,))["n"] == config.DAILY_REPORT_CAP


def test_lookup_cooldown(out):
    c = Chat(out).onboard(); c.text("AX2291"); c.text("EN3302")
    assert c.said == t("wait", "en")


# ---------------- robustness ----------------
def test_idle_session_resets_to_menu(out):
    c = Chat(out).onboard(); c.text("QQ1234Z");  assert c.session()["state"] == "AWAIT_EXPIRY"
    db.run("UPDATE wa_sessions SET last_seen_at = now() - interval '25 hours', last_lookup_at = NULL WHERE phone_hash=%s", (c.ph,))
    c.text("AX2291");  assert c.types == ["text", "audio", "interactive"] and "RED" in c.said     # treated as MENU, not as an expiry answer


def test_recent_session_keeps_flow(out):
    c = Chat(out).onboard(); c.text("QQ1234Z")
    c.text("AX2291");  assert c.said == t("ask_expiry", "en", b="QQ1234Z")


@pytest.mark.parametrize("kind", ["audio", "sticker", "location", "contacts", "video", "document"])
def test_unsupported_types(out, kind):
    c = Chat(out).onboard(); c.send(kind)
    assert c.said == t("unsupported", "en") and c.session()["state"] == "MENU"


def test_reaction_is_ignored_and_malformed_messages_skipped():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "w1", "from": "1", "type": "reaction", "reaction": {"emoji": "👍"}},
        {"from": "1", "type": "text"},                                    # no id
        {"id": "w2", "from": "1", "type": "text", "text": {}},            # no body
        {"id": "w3", "from": "1", "type": "image", "image": {}},          # no media id
        {"id": "w4", "from": "1", "type": "sticker"},
        {"id": "w5", "from": "1", "type": "text", "text": {"body": " hi "}}]}}]}, {"changes": None}, {}]}
    got = wa.parse_inbound(payload)
    assert [(i.wamid, i.kind, i.text) for i in got] == [("w4", "sticker", ""), ("w5", "text", "hi")]
    assert wa.parse_inbound({}) == [] and wa.parse_inbound({"entry": None}) == []


def test_concurrent_messages_same_number_do_not_corrupt_state(out):
    c = Chat(out).onboard()
    out.clear()
    def go(i):
        flow.handle(wa.Inbound(f"wamid.conc{uuid.uuid4().hex}", c.sender, "text", "AX2291"))
    ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [x.start() for x in ts]; [x.join() for x in ts]
    verdicts = [o for o in out if o["type"] == "audio"]
    assert len(verdicts) == 1                                              # one lookup, the other 11 got the cooldown reply
    assert sum(1 for o in out if o["type"] == "text" and t("wait", "en") in o["text"]["body"]) == 11
    assert c.session()["state"] == "MENU"


def test_same_wamid_processed_once(out):
    c = Chat(out).onboard(); m = wa.Inbound("wamid.dup1", c.sender, "text", "HELP")
    out.clear(); flow.handle(m); flow.handle(m)
    assert len(out) == 1
    db.run("DELETE FROM wa_inbound_dedupe WHERE wamid='wamid.dup1'")


def test_handler_exception_sends_fallback_in_users_language(out, monkeypatch):
    c = Chat(out).onboard("hi")
    monkeypatch.setattr(flow, "_handle", lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
    c.text("AX2291");  assert c.said == t("error", "hi")


def test_degraded_mode_prefixes_verdict(out, monkeypatch):
    monkeypatch.setattr(snapshot, "is_degraded", lambda: True)
    c = Chat(out).onboard("hi"); c.text("AX2291")
    assert c.said.startswith(t("degraded", "hi"))


def test_every_text_key_has_all_three_languages():
    import texts
    for k, d in texts.S.items():
        assert {"en", "hi", "kn"} <= set(d), k


# ---------------- wa.py: Graph errors, retry, media limits ----------------
@pytest.mark.parametrize("status,body,code,word", [
    (400, {"error": {"code": 131030, "message": "m"}}, 131030, "allow-list"),
    (400, {"error": {"code": 131047}}, 131047, "24-hour"),
    (400, {"error": {"code": 131053}}, 131053, "OGG"),
    (401, {"error": {"code": 190}}, 190, "token"),
    (401, {}, 190, "token"),
    (400, {"error": {"code": "abc"}}, None, "HTTP 400"),
    (500, None, None, "HTTP 500")])
def test_decode_error(status, body, code, word):
    d = wa.decode_error(status, body)
    assert d["code"] == code and word in d["hint"]


class _Resp:
    def __init__(self, status, body=None): self.status_code, self._b, self.content = status, body or {}, b"x"
    def json(self): return self._b


def test_send_retries_once_on_5xx_then_logs_never_raises(monkeypatch, capsys):
    monkeypatch.setattr(config, "DRY_RUN", False); monkeypatch.setattr(wa, "RETRY_DELAY_S", 0)
    calls = []
    monkeypatch.setattr(httpx, "request", lambda *a, **k: calls.append(1) or _Resp(500))
    assert wa._deliver({"messaging_product": "whatsapp", "to": "919900000001", "type": "text", "text": {"body": "hi"}}) == {}
    assert len(calls) == 2
    err = capsys.readouterr().out
    assert "919900000001" not in err and "wa_http_5xx" in err


def test_send_retry_succeeds_second_time(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False); monkeypatch.setattr(wa, "RETRY_DELAY_S", 0)
    seq = iter([httpx.ReadTimeout("t"), _Resp(200, {"messages": [{"id": "ok"}]})])
    def fake(*a, **k):
        r = next(seq)
        if isinstance(r, Exception): raise r
        return r
    monkeypatch.setattr(httpx, "request", fake)
    assert wa._deliver({"messaging_product": "whatsapp", "to": "919900000001", "type": "text", "text": {"body": "hi"}}) == {"messages": [{"id": "ok"}]}


def test_send_4xx_is_decoded_and_not_retried(monkeypatch, capsys):
    monkeypatch.setattr(config, "DRY_RUN", False)
    calls = []
    monkeypatch.setattr(httpx, "request", lambda *a, **k: calls.append(1) or _Resp(400, {"error": {"code": 131030, "message": "not allowed"}}))
    wa._deliver({"messaging_product": "whatsapp", "to": "919900000002", "type": "text", "text": {"body": "hi"}})
    assert len(calls) == 1
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["graph_code"] == 131030 and "allow-list" in line["hint"] and "919900000002" not in json.dumps(line)


def test_voice_note_missing_file_skips_quietly(out):
    assert wa.send_voice_note("919900000003", "/nonexistent/x.ogg") is None and out == []


def test_download_media_refuses_oversize(monkeypatch):
    monkeypatch.setattr(wa, "_request", lambda *a, **k: _Resp(200, {"url": "https://x/y", "file_size": 11 * 1024 * 1024}))
    with pytest.raises(wa.MediaTooLarge): wa.download_media("m1")
    monkeypatch.setattr(wa, "_request", lambda *a, **k: _Resp(200, {"url": "http://insecure/y"}))
    with pytest.raises(RuntimeError): wa.download_media("m1")


def test_mark_as_read_is_off_by_default(monkeypatch):
    monkeypatch.delenv("WA_MARK_AS_READ", raising=False)
    monkeypatch.setattr(wa, "_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call")))
    assert wa.mark_as_read("wamid.x") is None


# ---------------- log.py ----------------
def test_log_redacts_numbers_tokens_secrets(capsys, monkeypatch):
    monkeypatch.setattr(config, "WA_ACCESS_TOKEN", "EAAB-super-secret-token")
    log.event("t", phone="919876543210", sender="919876543210", note="call +91 98765 43210 now", auth="Bearer abc.def-123",
              leak="token is EAAB-super-secret-token", email="a@b.com", who="ab12cd34", n=7, big=919876543210, nested={"to": "1", "k": ["919876543210"]})
    line = capsys.readouterr().out
    for bad in ("9876543210", "98765 43210", "abc.def-123", "EAAB-super-secret-token", "a@b.com"):
        assert bad not in line, bad
    rec = json.loads(line)
    assert rec["event"] == "t" and rec["who"] == "ab12cd34" and rec["n"] == 7 and rec["phone"] == "[redacted]"


def test_log_never_raises(monkeypatch):
    monkeypatch.setattr("sys.stdout", None)
    log.event("x", a=object())


def test_flow_logs_use_hash_prefix_not_number(out, capsys):
    c = Chat(out).onboard(); c.text("AX2291")
    logs = capsys.readouterr().out
    assert c.sender not in logs and c.ph[:8] in logs and '"event": "inbound"' in logs and '"event": "verdict"' in logs
