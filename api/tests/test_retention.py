"""retention jobs, erase(), DELETE /api/user/data. Never touches origin='seed' rows."""
import io
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import db, jobs, privacy, ratelimit, storage
from routers import user

TAG = "w4ret" + uuid.uuid4().hex[:8]
IDENT = TAG + "-ident"
OTHER = TAG + "-other"

app = FastAPI()
app.include_router(user.router)
client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    ratelimit.reset()
    yield
    for sql in ("DELETE FROM uploads WHERE identity_hash LIKE %s", "DELETE FROM wa_sessions WHERE phone_hash LIKE %s",
                "DELETE FROM consent_log WHERE user_identifier_hash LIKE %s", "DELETE FROM wa_inbound_dedupe WHERE wamid LIKE %s",
                "DELETE FROM scan_events WHERE hashed_serial_id LIKE %s"):
        db.run(sql, (TAG + "%",))


def png() -> bytes:
    b = io.BytesIO(); Image.new("RGB", (8, 8), (9, 9, 9)).save(b, "PNG"); return b.getvalue()


def age_upload(ref, days):
    db.run("UPDATE uploads SET created_at = now() - make_interval(days => %s) WHERE id = %s", (days, ref))


def exists(ref):
    return bool(db.one("SELECT 1 FROM uploads WHERE id = %s", (ref,)))


def file_of(ref):
    return storage.upload_dir() / db.one("SELECT path FROM uploads WHERE id = %s", (ref,))["path"]


# ---------------- upload purge ----------------
def test_upload_purge_respects_attachment_and_age():
    old_free = storage.save_media(IDENT, "report", png(), "image/png")
    old_attached = storage.save_media(IDENT, "report", png(), "image/png")
    new_free = storage.save_media(IDENT, "dispute", png(), "image/png")
    for ref in (old_free, old_attached):
        age_upload(ref, 45)
    assert storage.attach(old_attached, "report", uuid.uuid4())
    f_old, f_att, f_new = file_of(old_free), file_of(old_attached), file_of(new_free)
    jobs.purge_unattached_uploads()
    assert not exists(old_free) and not f_old.exists()             # row and file gone
    assert exists(old_attached) and f_att.exists()                 # attached: kept
    assert exists(new_free) and f_new.exists()                     # recent: kept


# ---------------- dedupe / sessions ----------------
def test_dedupe_and_session_purge():
    db.run("INSERT INTO wa_inbound_dedupe (wamid, received_at) VALUES (%s, now() - interval '8 days'), (%s, now() - interval '6 days')",
           (TAG + "old", TAG + "new"))
    db.run("""INSERT INTO wa_sessions (phone_hash, last_seen_at) VALUES (%s, now() - interval '31 days'), (%s, now() - interval '29 days')""",
           (TAG + "idle", TAG + "active"))
    jobs.purge_dedupe(); jobs.purge_idle_sessions()
    assert not db.one("SELECT 1 FROM wa_inbound_dedupe WHERE wamid=%s", (TAG + "old",))
    assert db.one("SELECT 1 FROM wa_inbound_dedupe WHERE wamid=%s", (TAG + "new",))
    assert not db.one("SELECT 1 FROM wa_sessions WHERE phone_hash=%s", (TAG + "idle",))
    assert db.one("SELECT 1 FROM wa_sessions WHERE phone_hash=%s", (TAG + "active",))


# ---------------- location scrub ----------------
def _scan(name, origin, days):
    db.run("""INSERT INTO scan_events (id, hashed_serial_id, product_code, batch_number, approx_location, scanned_at, source, risk_level, origin)
              VALUES (gen_random_uuid(), %s, 'x', 'W4RET', 'Bengaluru 560001', now() - make_interval(days => %s), 'whatsapp', 'green', %s)""",
           (TAG + name, days, origin))


def loc(name):
    return db.one("SELECT approx_location AS l FROM scan_events WHERE hashed_serial_id = %s", (TAG + name,))["l"]


def test_location_scrub_never_touches_seed():
    seed_before = db.one("SELECT count(*) AS n, md5(string_agg(id::text || approx_location, ',' ORDER BY id)) AS h FROM scan_events WHERE origin='seed'")
    _scan("live_old", "live", 200); _scan("live_new", "live", 10); _scan("seed_old", "seed", 400)
    assert jobs.scrub_locations() >= 1
    assert loc("live_old") == "scrubbed"
    assert loc("live_new") == "Bengaluru 560001"
    assert loc("seed_old") == "Bengaluru 560001"                   # seed rows untouched even when ancient
    seed_after = db.one("SELECT count(*) AS n, md5(string_agg(id::text || approx_location, ',' ORDER BY id)) AS h FROM scan_events WHERE origin='seed'")
    assert seed_after["n"] == seed_before["n"] + 1
    db.run("DELETE FROM scan_events WHERE hashed_serial_id = %s", (TAG + "seed_old",))
    seed_final = db.one("SELECT count(*) AS n, md5(string_agg(id::text || approx_location, ',' ORDER BY id)) AS h FROM scan_events WHERE origin='seed'")
    assert seed_final == seed_before


def test_seed_rows_untouched_by_run_all():
    before = db.one("SELECT count(*) AS n, md5(string_agg(id::text || approx_location || scanned_at::text, ',' ORDER BY id)) AS h FROM scan_events WHERE origin='seed'")
    out = jobs.run_all()
    assert set(out) == {"purge_unattached_uploads", "purge_dedupe", "purge_idle_sessions", "scrub_locations"} and None not in out.values()
    assert db.one("SELECT count(*) AS n, md5(string_agg(id::text || approx_location || scanned_at::text, ',' ORDER BY id)) AS h FROM scan_events WHERE origin='seed'") == before


def test_scheduler_gated_by_env(monkeypatch):
    monkeypatch.delenv("ENABLE_SCHEDULER", raising=False)
    assert jobs.start() is None
    monkeypatch.setenv("ENABLE_SCHEDULER", "1")
    s = jobs.start()
    try:
        assert s is not None and s.get_job("retention")
    finally:
        jobs.stop()


# ---------------- erase ----------------
def _seed_user(ident):
    db.run("INSERT INTO wa_sessions (phone_hash) VALUES (%s)", (ident,))
    for _ in range(2):
        db.run("""INSERT INTO consent_log (id, user_identifier_hash, consent_type, granted_at, channel)
                  VALUES (gen_random_uuid(), %s, 'data_processing', now(), 'whatsapp')""", (ident,))


def test_erase_counts_and_scope():
    _seed_user(IDENT); _seed_user(OTHER)
    a = storage.save_media(IDENT, "report", png(), "image/png")
    b = storage.save_media(IDENT, "report", png(), "image/png")
    attached = storage.save_media(IDENT, "report", png(), "image/png")
    theirs = storage.save_media(OTHER, "report", png(), "image/png")
    storage.attach(attached, "report", uuid.uuid4())
    fa, fb, fatt, fth = file_of(a), file_of(b), file_of(attached), file_of(theirs)
    assert privacy.erase(IDENT) == {"session": 1, "consent": 2, "photos_removed": 2}
    assert not exists(a) and not exists(b) and not fa.exists() and not fb.exists()
    assert exists(attached) and fatt.exists()                      # filed report keeps its evidence
    assert exists(theirs) and fth.exists()                         # other users untouched
    assert db.one("SELECT count(*) AS n FROM wa_sessions WHERE phone_hash=%s", (OTHER,))["n"] == 1
    assert privacy.erase(IDENT) == {"session": 0, "consent": 0, "photos_removed": 0}     # idempotent


def test_delete_user_data_endpoint():
    h = "a" * 64
    assert client.delete("/api/user/data").status_code == 400
    assert client.delete("/api/user/data", headers={"X-Identity-Hash": "not-a-hash"}).status_code == 400
    r = client.delete("/api/user/data", headers={"X-Identity-Hash": h})
    assert r.status_code == 200 and r.json() == {"session": 0, "consent": 0, "photos_removed": 0}
