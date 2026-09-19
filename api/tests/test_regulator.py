"""regulator router (reports, disputes, audit) + public dispute banner. Own FastAPI app."""
import uuid
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db, engine, ratelimit
from routers import disputes, regulator

SECRET = "w4-test-jwt-secret-0123456789-abcdefghij"
USER, PW = "w4-test-regulator", "w4-test-shared-secret-value"
TAG = "W4R" + uuid.uuid4().hex[:6].upper()          # batch prefix for rows this file owns

app = FastAPI()
app.include_router(regulator.router)
app.include_router(disputes.router)
client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("REGULATOR_USERNAME", USER)
    monkeypatch.setenv("REGULATOR_SHARED_SECRET", PW)
    monkeypatch.delenv("SLA_DAYS", raising=False)
    ratelimit.reset()


@pytest.fixture
def hdr():
    tok = client.post("/api/regulator/login", json={"username": USER, "secret": PW}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _cleanup():
    db.run("DELETE FROM reports WHERE scan_id IN (SELECT id FROM scan_events WHERE hashed_serial_id LIKE 'w4test%%')")
    db.run("DELETE FROM scan_events WHERE hashed_serial_id LIKE 'w4test%%'")
    db.run("DELETE FROM disputes WHERE batch_number LIKE %s", (TAG + "%",))
    db.run("DELETE FROM nsq_alerts WHERE batch_number LIKE %s", (TAG + "%",))
    db.run("DELETE FROM batches WHERE batch_number LIKE %s", (TAG + "%",))
    db.run("DELETE FROM audit_log WHERE actor = %s OR (actor = 'anonymous' AND action LIKE 'regulator:%%')", (USER,))


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def mk_batch(suffix, nsq_active=True):
    b = f"{TAG}{suffix}"
    pc = db.one("SELECT product_code FROM products LIMIT 1")["product_code"]
    db.run("""INSERT INTO batches (id, product_code, batch_number, manufacturing_date, expiry_date, manufacturer)
              VALUES (gen_random_uuid(), %s, %s, %s, %s, 'W4 Test Pharma')""",
           (pc, b, date.today() - timedelta(days=100), date.today() + timedelta(days=400)))
    if nsq_active:
        db.run("""INSERT INTO nsq_alerts (id, product_name, batch_number, manufacturer, alert_date, reason, source_document,
                                         extraction_confidence, lab_type, status)
                  VALUES (gen_random_uuid(), 'W4 test product', %s, 'W4 Test Pharma', current_date, 'test reason', 'w4-test.pdf', 'high', 'central', 'active')""", (b,))
    return b


def mk_dispute(batch, days_old=0, status="open"):
    did = uuid.uuid4()
    db.run("""INSERT INTO disputes (id, batch_number, submitted_by, submitter_domain, status, submitted_at, channel)
              VALUES (%s, %s, 'legal@w4test.example', 'w4test.example', %s, now() - make_interval(days => %s), 'whatsapp')""",
           (did, batch, status, days_old))
    return did


def mk_report(status="open"):
    sid, rid = uuid.uuid4(), uuid.uuid4()
    db.run("""INSERT INTO scan_events (id, hashed_serial_id, product_code, batch_number, approx_location, scanned_at, source, risk_level)
              VALUES (%s, %s, 'x', %s, 'n/a', now(), 'whatsapp', 'green')""", (sid, "w4test" + uuid.uuid4().hex, TAG + "R"))
    db.run("""INSERT INTO reports (id, scan_id, report_type, description, reporter_identity_hash, created_at, status)
              VALUES (%s, %s, 'suspicious', 'w4 test', 'w4testhash', now(), %s)""", (rid, sid, status))
    return rid


# ---------------- reports ----------------
def test_report_transitions(hdr):
    rid = mk_report()
    url = f"/api/regulator/reports/{rid}/status"
    post = lambda s: client.post(url, json={"status": s}, headers=hdr)
    assert post("confirmed").status_code == 409                    # open -> confirmed not allowed
    assert post("dismissed").status_code == 409
    assert post("open").status_code == 422                         # not a settable target
    assert post("under_investigation").status_code == 200
    assert post("under_investigation").status_code == 409          # same state
    assert post("confirmed").status_code == 200
    assert post("dismissed").status_code == 409                    # terminal
    assert db.one("SELECT status FROM reports WHERE id=%s", (rid,))["status"] == "confirmed"
    rid2 = mk_report("under_investigation")
    assert client.post(f"/api/regulator/reports/{rid2}/status", json={"status": "dismissed"}, headers=hdr).status_code == 200
    assert client.post(f"/api/regulator/reports/{uuid.uuid4()}/status", json={"status": "under_investigation"}, headers=hdr).status_code == 404


def test_list_reports_filter_and_no_identity_leak(hdr):
    rid = mk_report("open")
    rows = client.get("/api/regulator/reports?status=open&limit=500", headers=hdr).json()["reports"]
    mine = [r for r in rows if r["id"] == str(rid)]
    assert mine and "reporter_identity_hash" not in mine[0] and mine[0]["batch_number"] == TAG + "R"
    assert all(r["status"] == "open" for r in rows)
    assert not [r for r in client.get("/api/regulator/reports?status=dismissed&limit=500", headers=hdr).json()["reports"] if r["id"] == str(rid)]
    assert client.get("/api/regulator/reports?status=bogus", headers=hdr).status_code == 422


# ---------------- disputes ----------------
def test_overturn_flips_red_to_green_and_sets_statuses(hdr):
    b = mk_batch("A")
    d = mk_dispute(b)
    v, _ = engine.verify_manual(b)
    assert v.verdict == "RED" and v.dispute_open                  # dispute alone never changes the verdict
    r = client.post(f"/api/regulator/disputes/{d}/resolve", json={"resolution": "overturned", "notes": "lab retest passed"}, headers=hdr)
    assert r.status_code == 200 and r.json()["status"] == "resolved_overturned"
    assert db.one("SELECT status FROM nsq_alerts WHERE batch_number=%s", (b,))["status"] == "corrected"
    assert db.one("SELECT dispute_status FROM batches WHERE batch_number=%s", (b,))["dispute_status"] == "resolved_overturned"
    row = db.one("SELECT status, resolution_notes, resolved_at FROM disputes WHERE id=%s", (d,))
    assert row["status"] == "resolved_overturned" and row["resolution_notes"] == "lab retest passed" and row["resolved_at"]
    v, _ = engine.verify_manual(b)
    assert v.verdict == "GREEN" and not v.dispute_open
    assert any("corrected" in s.evidence_text for s in v.signals if s.signal_type == "nsq_status")   # history stays visible
    again = client.post(f"/api/regulator/disputes/{d}/resolve", json={"resolution": "upheld", "notes": "x"}, headers=hdr)
    assert again.status_code == 409


def test_upheld_keeps_red(hdr):
    b = mk_batch("B")
    d = mk_dispute(b)
    r = client.post(f"/api/regulator/disputes/{d}/resolve", json={"resolution": "upheld", "notes": "listing stands"}, headers=hdr)
    assert r.status_code == 200 and r.json()["status"] == "resolved_upheld"
    assert db.one("SELECT status FROM nsq_alerts WHERE batch_number=%s", (b,))["status"] == "active"
    assert db.one("SELECT dispute_status FROM batches WHERE batch_number=%s", (b,))["dispute_status"] == "resolved_upheld"
    assert engine.verify_manual(b)[0].verdict == "RED"


def test_resolve_validation(hdr):
    b = mk_batch("C"); d = mk_dispute(b)
    url = f"/api/regulator/disputes/{d}/resolve"
    assert client.post(url, json={"resolution": "maybe", "notes": "x"}, headers=hdr).status_code == 422
    assert client.post(url, json={"resolution": "upheld", "notes": ""}, headers=hdr).status_code == 422
    assert client.post(f"/api/regulator/disputes/{uuid.uuid4()}/resolve", json={"resolution": "upheld", "notes": "x"}, headers=hdr).status_code == 404
    assert db.one("SELECT status FROM disputes WHERE id=%s", (d,))["status"] == "open"     # nothing changed


def test_overdue_filter(hdr):
    b = mk_batch("D", nsq_active=False)
    old, fresh = mk_dispute(b, days_old=10), mk_dispute(b, days_old=1)
    old_resolved = mk_dispute(b, days_old=30, status="resolved_upheld")
    ids = lambda q: {d["id"] for d in client.get("/api/regulator/disputes?limit=500&" + q, headers=hdr).json()["disputes"]}
    mine = {str(old), str(fresh), str(old_resolved)}
    assert ids("overdue=true") & mine == {str(old)}                # resolved disputes are never overdue
    assert ids("overdue=false") & mine == {str(fresh), str(old_resolved)}
    assert ids("status=open") & mine == {str(old), str(fresh)}
    assert ids("status=open&overdue=true") & mine == {str(old)}
    assert ids("") & mine == mine
    assert client.get("/api/regulator/disputes", headers=hdr).json()["sla_days"] == 7


def test_sla_days_env(hdr, monkeypatch):
    b = mk_batch("E", nsq_active=False); d = mk_dispute(b, days_old=3)
    monkeypatch.setenv("SLA_DAYS", "2")
    assert str(d) in {x["id"] for x in client.get("/api/regulator/disputes?overdue=true&limit=500", headers=hdr).json()["disputes"]}
    monkeypatch.setenv("SLA_DAYS", "junk")                         # bad value falls back to 7
    assert str(d) not in {x["id"] for x in client.get("/api/regulator/disputes?overdue=true&limit=500", headers=hdr).json()["disputes"]}


# ---------------- audit ----------------
def _audit_count(where, params=()):
    return db.one(f"SELECT count(*) AS n FROM audit_log WHERE {where}", params)["n"]


def test_every_regulator_call_writes_audit_rows(hdr):
    # login (fixture did one successful login)
    assert _audit_count("actor=%s AND action='regulator:login ok'", (USER,)) == 1
    client.post("/api/regulator/login", json={"username": USER, "secret": "wrong"})
    assert _audit_count("actor='anonymous' AND action='regulator:login failed'") >= 1
    rid = mk_report()
    client.get("/api/regulator/reports?status=open", headers=hdr)
    client.get("/api/regulator/disputes?overdue=true", headers=hdr)
    client.post(f"/api/regulator/reports/{rid}/status", json={"status": "under_investigation"}, headers=hdr)
    b = mk_batch("F"); d = mk_dispute(b)
    client.post(f"/api/regulator/disputes/{d}/resolve", json={"resolution": "upheld", "notes": "n"}, headers=hdr)
    acts = [r["action"] + " | " + r["target"] for r in db.all_("SELECT action, target FROM audit_log WHERE actor=%s", (USER,))]
    for needle in ("GET /api/regulator/reports | /api/regulator/reports?status=open",
                   "GET /api/regulator/disputes | /api/regulator/disputes?overdue=true",
                   "POST /api/regulator/reports/{report_id}/status",
                   "regulator:report_status | report:%s open->under_investigation" % rid,
                   "POST /api/regulator/disputes/{dispute_id}/resolve",
                   "regulator:dispute_resolve | dispute:%s resolved_upheld" % d):
        assert any(needle in a for a in acts), needle
    # rejected calls are audited too
    before = _audit_count("actor='anonymous' AND action LIKE 'regulator:GET%%denied(401)'")
    assert client.get("/api/regulator/reports").status_code == 401
    assert _audit_count("actor='anonymous' AND action LIKE 'regulator:GET%%denied(401)'") == before + 1


# ---------------- public banner ----------------
def test_public_dispute_banner_exposes_no_private_data():
    b = mk_batch("G")
    r = client.get(f"/api/disputes/{b.lower()}")
    assert r.status_code == 200 and r.json()["dispute_open"] is False and r.json()["banner"] is None
    mk_dispute(b)
    r = client.get(f"/api/disputes/{b}?lang=hi")
    j = r.json()
    assert j["dispute_open"] is True and j["banner"] and set(j) == {"batch_number", "dispute_open", "dispute_status", "banner"}
    assert "w4test.example" not in r.text
    assert client.get("/api/disputes/NOPE-NOT-A-BATCH").status_code == 404
