"""timeline text/JSON, replay preview, regulator-demo logic. Needs the seeded DB (DATABASE_URL), e.g. a scratch database."""
import json
import os
import sys
import uuid
import zlib

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db
import engine
import replay
import timeline
from routers import timeline as timeline_router

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import regulator_demo  # noqa: E402


@pytest.fixture
def restore_demo():
    yield
    regulator_demo.reset()


# ---------------- timeline text ----------------
def test_timeline_en_sections_in_order():
    t = timeline.format_timeline("AX2291", "en")
    marks = ["Timeline — batch AX2291", "Manufactured by", "Official CDSCO records", "ACTIVE",
             "Scan activity by district", "synthetic", "Community reports", "GREEN never means genuine"]
    pos = [t.index(m) for m in marks]
    assert pos == sorted(pos)


def test_timeline_hi_kn_and_unknown_language():
    assert "टाइमलाइन" in timeline.format_timeline("AX2291", "hi")
    assert "ಟೈಮ್‌ಲೈನ್" in timeline.format_timeline("AX2291", "kn")
    assert timeline.format_timeline("AX2291", "fr") == timeline.format_timeline("AX2291", "en")


def test_missing_translations_fall_back_to_english(monkeypatch):
    # RV5567 shows SUPERSEDED + CORRECTED; kn has neither status string, and 'scan_more' -> English, never a crash.
    t = timeline.format_timeline("RV5567", "kn")
    assert "ಟೈಮ್‌ಲೈನ್" in t and "SUPERSEDED" in t and "CORRECTED" in t and "more districts" in t
    monkeypatch.setitem(timeline.STR, "title", {"en": "EN title {b}"})   # even a whole missing language block
    assert "EN title RV5567" in timeline.format_timeline("RV5567", "hi")


def test_unknown_batch():
    assert "No records found for batch ZZ0000" in timeline.format_timeline("zz0000", "en")
    assert timeline.timeline_json("ZZ0000")["found"] is False


def test_corrected_and_superseded_history_shown():
    t = timeline.format_timeline("RV5567", "en")
    assert t.index("SUPERSEDED") < t.index("CORRECTED")          # chronological (Nov 2025 then Dec 2025)
    assert "no longer an active listing" in t
    js = timeline.timeline_json("RV5567")
    assert [e["status"] for e in js["events"] if e["kind"] == "nsq_alert"] == ["superseded", "corrected"]


def test_scan_activity_is_aggregated_and_labelled_synthetic():
    js = timeline.timeline_json("AX2291")
    (scan,) = [e for e in js["events"] if e["kind"] == "scan_activity"]
    assert scan["synthetic"] and scan["total"] == sum(d["scans"] for d in scan["districts"])
    assert "synthetic" in js["synthetic_notice"]


def test_disputes_filed_and_resolved_shown():
    t = timeline.format_timeline("SW1123", "en")
    assert "Dispute filed by" in t and "OVERTURNED" in t
    kinds = [e["kind"] for e in timeline.timeline_json("SW1123")["events"]]
    assert kinds[-2:] == ["dispute_filed", "dispute_resolved"]


def test_report_counts_never_leak():
    b = db.one("SELECT s.batch_number FROM reports r JOIN scan_events s ON s.id = r.scan_id GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")["batch_number"]
    before_txt, before_js = timeline.format_timeline(b, "en"), json.dumps(timeline.timeline_json(b), sort_keys=True)
    sid = db.one("SELECT id FROM scan_events WHERE batch_number = %s LIMIT 1", (b,))["id"]
    ids = [uuid.uuid4() for _ in range(7)]
    try:
        for i in ids:
            db.run("""INSERT INTO reports (id, scan_id, report_type, description, reporter_identity_hash, created_at)
                      VALUES (%s, %s, 'suspicious', 'x', 'h', now())""", (i, sid))
        assert timeline.format_timeline(b, "en") == before_txt
        assert json.dumps(timeline.timeline_json(b), sort_keys=True) == before_js
    finally:
        db.run("DELETE FROM reports WHERE id = ANY(%s)", (ids,))
    assert "report" not in before_js.lower()
    for lang in ("en", "hi", "kn"):
        assert timeline.format_timeline(b, lang).count("\n") > 5   # non-empty in every language; report line is fixed text


def test_router():
    app = FastAPI()
    app.include_router(timeline_router.router)
    c = TestClient(app)
    r = c.get("/api/batch-timeline/ax2291")
    assert r.status_code == 200 and r.json()["batch"] == "AX2291" and r.json()["found"]
    assert "report" not in r.text.lower()
    assert c.get("/api/batch-timeline/ZZ0000").status_code == 404
    assert c.get("/api/batch-timeline/bad%20batch!").status_code == 400


# ---------------- replay preview ----------------
def _ins(serial, batch, loc, ts):
    db.run("""INSERT INTO scan_events (id, hashed_serial_id, product_code, batch_number, approx_location, scanned_at, source, risk_level, origin)
              SELECT gen_random_uuid(), %s, product_code, batch_number, %s, %s::timestamptz, 'whatsapp', 'green', 'seed'
                FROM batches WHERE batch_number = %s""", (replay.serial_hash(serial), loc, ts, batch))


def test_serial_hash_is_sha256_32():
    import hashlib
    assert replay.serial_hash("SN1") == hashlib.sha256(b"SN1").hexdigest()[:32]


def test_serial_level_flagged_and_not_flagged():
    s = "TEST-SERIAL-" + uuid.uuid4().hex[:8]
    h = replay.serial_hash(s)
    try:
        assert replay.replay_signal("MQ7756", s).status == "INCONCLUSIVE"            # no events yet
        _ins(s, "MQ7756", "110001 (New Delhi, Delhi)", "2026-01-01 08:00+00")
        _ins(s, "MQ7756", "110002 (New Delhi, Delhi)", "2026-01-01 10:00+00")       # same state
        assert replay.replay_signal("MQ7756", s).status == "INCONCLUSIVE"
        _ins(s, "MQ7756", "560034 (Bengaluru Urban, Karnataka)", "2026-01-03 10:00+00")   # different state but >24 h apart
        assert replay.replay_signal("MQ7756", s).status == "INCONCLUSIVE"
        _ins(s, "MQ7756", "400072 (Mumbai Suburban, Maharashtra)", "2026-01-01 20:00+00")  # different state within 24 h
        sig = replay.replay_signal("MQ7756", s)
        assert sig.status == "FLAGGED" and sig.synthetic and "two different states" in sig.evidence_text
        assert replay.replay_signal("MQ7756").status == "INCONCLUSIVE"                # batch-level alone: no tag on MQ7756
    finally:
        db.run("DELETE FROM scan_events WHERE hashed_serial_id = %s", (h,))


def test_demo_pack_serial_is_flagged_at_serial_level():
    serial = f"SN{zlib.crc32(b'CT5510') % 10**8:08d}"      # same formula as make_demo_packs.py
    assert replay._serial_flagged(serial)
    assert not replay._serial_flagged(f"SN{zlib.crc32(b'MQ7756') % 10**8:08d}")   # GREEN pack must stay clean
    assert replay.replay_signal("CT5510", serial).status == "FLAGGED"


def test_batch_level_fallback_and_labels():
    assert replay.replay_signal("JK4471").status == "FLAGGED"                       # seed tag impossible_travel_demo
    assert replay.replay_signal("JK4471", "NO-SUCH-SERIAL").status == "FLAGGED"     # falls back to batch level
    assert replay.replay_signal("MQ7756").status == "INCONCLUSIVE"
    nothing = replay.replay_signal("ZZ0000", "x")
    assert nothing.status == "INCONCLUSIVE" and "Nothing to check" in nothing.evidence_text
    for sig in (replay.replay_signal(b) for b in ("JK4471", "MQ7756", "ZZ0000")):
        assert sig.evidence_text.startswith("[Preview]") and "synthetic demo data" in sig.evidence_text
        assert sig.signal_type == "replay_pattern" and sig.synthetic and not sig.official


def test_serial_replay_plus_label_mismatch_escalates():
    sig = replay.replay_signal("MQ7756", None)
    assert sig.status != "FLAGGED"
    flagged = replay.replay_signal("CT5510", f"SN{zlib.crc32(b'CT5510') % 10**8:08d}")
    label = engine.Signal("label_consistency", "FAIL", "x", "ocr_reading", "r", "medium")
    assert engine.resolve_conflicts([label, flagged]).reason == "escalated_conflict"


# ---------------- fixups ----------------
def test_fixups_idempotent_and_seed_origin(restore_demo):
    def n():
        return db.one("SELECT count(*) AS n, count(*) FILTER (WHERE origin = 'seed') AS s FROM scan_events "
                      "WHERE 'serial_replay_demo' = ANY(risk_reasons)")
    total = db.one("SELECT count(*) AS n FROM scan_events")["n"]
    regulator_demo.reset(); regulator_demo.reset()
    assert n() == {"n": 2, "s": 2}
    assert db.one("SELECT count(*) AS n FROM scan_events")["n"] == total


# ---------------- regulator demo ----------------
def test_demo_logic_against_db_overturn_then_reset(restore_demo):
    regulator_demo.reset()
    assert engine.verify_manual("AX2291")[0].verdict == "RED"
    # What resolve(overturned) does to the data, done directly:
    db.run("UPDATE nsq_alerts SET status = 'corrected' WHERE batch_number = 'AX2291'")
    db.run("""INSERT INTO disputes (id, batch_number, submitted_by, submitter_domain, status, submitted_at, resolved_at, resolution_notes)
              VALUES (gen_random_uuid(), 'AX2291', 'Amrutha Drugs Ltd.', 'amruthadrugs.com', 'resolved_overturned', now() - interval '1 day', now(), 'Evidence accepted')""")
    assert engine.verify_manual("AX2291")[0].verdict == "GREEN"
    t = timeline.format_timeline("AX2291", "en")
    assert "CORRECTED" in t and "OVERTURNED" in t and "Evidence accepted" in t
    regulator_demo.reset()
    assert engine.verify_manual("AX2291")[0].verdict == "RED"
    assert db.one("SELECT count(*) AS n FROM disputes WHERE batch_number = 'AX2291'")["n"] == 0
    assert "OVERTURNED" not in timeline.format_timeline("AX2291", "en")


def test_demo_script_http_flow_against_contract(capsys):
    state = {"verdict": "RED", "calls": []}

    def handler(req: httpx.Request) -> httpx.Response:
        state["calls"].append((req.method, req.url.path))
        p = req.url.path
        if p == "/api/lookup-batch/AX2291":
            return httpx.Response(200, json={"verdict": state["verdict"]})
        if p == "/api/regulator/login":
            return httpx.Response(200, json={"access_token": "tok-SECRET-abc"})
        if p == "/api/regulator/disputes":
            assert req.headers["authorization"] == "Bearer tok-SECRET-abc"
            rows = [{"id": "11111111-2222", "batch_number": "AX2291", "status": "open"}] if req.url.params["status"] == "open" else []
            return httpx.Response(200, json=rows)
        if p == "/api/regulator/disputes/11111111-2222/resolve":
            assert json.loads(req.content) == {"resolution": "overturned", "notes": "n"}
            state["verdict"] = "GREEN"
            return httpx.Response(200, json={"status": "resolved_overturned"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        before, after = regulator_demo.run(c, "http://x", "AX2291", "overturned", "n", "reg", "hunter2-secret", show_timeline=False)
    assert (before, after) == ("RED", "GREEN")
    out = capsys.readouterr().out
    assert "RED -> GREEN" in out and "hunter2-secret" not in out and "tok-SECRET" not in out
