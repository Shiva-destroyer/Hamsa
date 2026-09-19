"""things going wrong -- DB down, malformed / huge / partial webhook bodies, unsupported types, replays, forged signatures,
Graph 5xx. The bot must always answer the webhook with 200 (or 403 for a bad signature), never crash, never leak numbers.
Needs the seeded DB (DATABASE_URL), e.g. a scratch database. In-process; no network."""
import json, os, sys, uuid
import httpx
import pytest
from fastapi.testclient import TestClient
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import harness
import app as appmod, config, db, flow, snapshot, wa
from texts import t, VERDICT

client = TestClient(appmod.app)


@pytest.fixture
def chat(monkeypatch):
    c = harness.Chat(mp=monkeypatch); c.wipe(); yield c; c.wipe()


def post_raw(raw: bytes, sig="sign"):
    """sig: "sign" = correctly signed, None = header omitted, anything else is sent verbatim."""
    h = {"Content-Type": "application/json"}
    if sig is not None: h["X-Hub-Signature-256"] = harness.sign(raw) if sig == "sign" else sig
    return client.post("/webhook", content=raw, headers=h)


def msg(sender, **kw): return {"from": sender, "id": "wamid." + uuid.uuid4().hex, "timestamp": "1", **kw}


# ---------------- DB down ----------------
def _nsq_down(monkeypatch):
    real = db.all_
    def flaky(sql, params=()):
        if "nsq_alerts" in sql: raise RuntimeError("db down")
        return real(sql, params)
    monkeypatch.setattr(db, "all_", flaky)


def test_official_list_down_with_snapshot_answers_from_cache_with_warning(chat, monkeypatch):
    snapshot.load_snapshot(); chat.onboard("en"); _nsq_down(monkeypatch)
    chat.text("AX2291")
    assert chat.said.startswith(t("degraded", "en")) and "Data as of" in chat.said and "RED —" in chat.said
    snapshot.set_degraded(False)


def test_official_list_down_without_snapshot_is_amber_never_green(chat, monkeypatch):
    chat.onboard("hi"); _nsq_down(monkeypatch)
    monkeypatch.setattr(snapshot, "_date", None); monkeypatch.setattr(snapshot, "_rows", [])
    chat.text("MQ7756")                                                     # a batch that is GREEN when the list is reachable
    assert chat.said.startswith(t("degraded", "hi")) and VERDICT["AMBER"]["head"]["hi"] in chat.said and "GREEN" not in chat.said.split("\n")[2]
    snapshot.set_degraded(False)


def test_whole_database_down_webhook_still_200_and_user_gets_apology(chat, monkeypatch):
    def down(*a, **k): raise RuntimeError("connection refused")
    with monkeypatch.context() as m:
        m.setattr(db, "conn", down)
        r = post_raw(harness.webhook_payload(msg(chat.sender, type="text", text={"body": "AX2291"})))
    assert r.status_code == 200
    assert chat.said == t("error", "en")


# ---------------- hostile / broken webhook bodies ----------------
@pytest.mark.parametrize("raw", [b"{not json", b"", b"[]", b"null", b'"str"', b"123", json.dumps({"entry": "x"}).encode(),
                                 json.dumps({"entry": [None, 5, {"changes": "y"}]}).encode(), json.dumps({"entry": [{"changes": [{"value": {"messages": "z"}}]}]}).encode()])
def test_signed_garbage_bodies_are_ignored_with_200(chat, raw):
    r = post_raw(raw)
    assert r.status_code == 200 and chat.out == []


def test_huge_signed_body_is_survived(chat):
    pad = "x" * (3 * 1024 * 1024)
    r = post_raw(json.dumps({"entry": [], "pad": pad}).encode())
    assert r.status_code in (200, 413) and chat.out == []


def test_huge_text_message_is_handled(chat):
    chat.onboard("en"); chat.text("A" * 200_000)
    assert chat.said == t("unknown", "en")


def test_messages_with_missing_fields_are_skipped_the_rest_processed(chat):
    good = msg(chat.sender, type="text", text={"body": "hi"})
    raw = harness.webhook_payload({"type": "text"}, {"id": "w1", "type": "text", "text": {}}, {"id": "w2", "from": chat.sender, "type": "image", "image": {}}, good)
    assert post_raw(raw).status_code == 200
    assert "Do you agree" in chat.said                                       # only the well-formed message produced a reply


@pytest.mark.parametrize("kind,extra", [("audio", {"audio": {"id": "a1"}}), ("sticker", {"sticker": {"id": "s1"}}),
                                         ("location", {"location": {"latitude": 1, "longitude": 2}}), ("contacts", {"contacts": [{}]}),
                                         ("video", {"video": {"id": "v1"}}), ("weird_new_type", {})])
def test_unsupported_types_via_webhook(chat, kind, extra):
    chat.onboard("en"); chat.out.clear()
    assert post_raw(harness.webhook_payload(msg(chat.sender, type=kind, **extra))).status_code == 200
    assert chat.said == t("unsupported", "en")


def test_reaction_gets_no_reply(chat):
    chat.onboard("en"); harness._BOX[chat.sender].clear()
    assert post_raw(harness.webhook_payload(msg(chat.sender, type="reaction", reaction={"emoji": "👍"}))).status_code == 200
    assert chat.out == []


def test_delivery_status_payloads_are_ignored(chat):
    raw = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.x", "status": "delivered"}]}}]}]}).encode()
    assert post_raw(raw).status_code == 200 and chat.out == []


# ---------------- replays and forgeries ----------------
def test_replayed_wamid_is_processed_once(chat):
    m = msg(chat.sender, type="text", text={"body": "hi"}); raw = harness.webhook_payload(m)
    post_raw(raw); n = len(chat.out); post_raw(raw)
    assert n == 1 and len(chat.out) == n


@pytest.mark.parametrize("sig", [None, "", "sha256=", "sha256=" + "0" * 64, "md5=abc", "garbage"])
def test_forged_or_missing_signature_is_403_and_silent(chat, sig):
    raw = harness.webhook_payload(msg(chat.sender, type="text", text={"body": "hi"}))
    assert post_raw(raw, sig=sig).status_code == 403 and chat.out == []


def test_signature_over_a_different_body_is_rejected(chat):
    a = harness.webhook_payload(msg(chat.sender, type="text", text={"body": "hi"}))
    b = harness.webhook_payload(msg(chat.sender, type="text", text={"body": "AX2291"}))
    assert post_raw(b, sig=harness.sign(a)).status_code == 403 and chat.out == []


# ---------------- Graph API misbehaves ----------------
class _R:
    def __init__(self, status): self.status_code, self.content = status, b"{}"
    def json(self): return {"error": {"code": 131047, "message": "window closed"}}


def test_graph_500s_are_retried_once_then_logged_and_the_handler_survives(chat, monkeypatch, capsys):
    chat.onboard("en")
    calls = []
    monkeypatch.setattr(config, "DRY_RUN", False); monkeypatch.setattr(wa, "RETRY_DELAY_S", 0)
    monkeypatch.setattr(wa, "_post", wa._deliver)                                        # real send path, fake network
    monkeypatch.setattr(httpx, "request", lambda *a, **k: calls.append(1) or _R(500))
    capsys.readouterr()
    flow.handle(wa.Inbound("wamid." + uuid.uuid4().hex, chat.sender, "text", "HELP"))    # one send -> 2 attempts
    out = capsys.readouterr().out
    assert len(calls) == 2 and "wa_http_5xx" in out and "wa_send_failed" in out
    assert chat.sender not in out and chat.ph[:8] in out
    assert chat.session()["state"] == "MENU"


def test_graph_window_closed_error_is_decoded_in_the_log(chat, monkeypatch, capsys):
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(httpx, "request", lambda *a, **k: _R(400))
    capsys.readouterr()
    wa._deliver({"messaging_product": "whatsapp", "to": chat.sender, "type": "text", "text": {"body": "x"}})
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["graph_code"] == 131047 and "24-hour" in line["hint"] and chat.sender not in json.dumps(line)
