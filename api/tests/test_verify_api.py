"""POST /api/verify: request/response shape, all three input types, voice URL + cache fields, degraded mode, rejection of bad input.
Builds its own FastAPI app around the router (app.py wiring is the master's job). Needs the seeded DB (DATABASE_URL)."""
import base64
import io
import os
from datetime import date

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import db
import engine
import snapshot
from routers import verify as verify_mod
from test_vision import G, gs1, pack_for, reg, render_pack, to_bytes  # noqa: F401  (reg is a fixture)

PACKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "packs")
app = FastAPI()
app.include_router(verify_mod.router)
client = TestClient(app)

TOP_KEYS = {"verdict", "verdict_reason", "also_expired", "dispute_open", "evidence_matrix", "safe_action", "served_from_cache", "cache_last_updated"}
ROW_KEYS = {"signal_type", "status", "evidence_text", "source_type", "source_reference", "confidence", "last_updated", "official", "synthetic", "dispute"}


def post(**body):
    return client.post("/api/verify", json=body)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


@pytest.fixture(autouse=True)
def clean_snapshot(monkeypatch):
    monkeypatch.setattr(snapshot, "_rows", [])
    monkeypatch.setattr(snapshot, "_date", None)
    monkeypatch.setattr(snapshot, "_degraded", False)


@pytest.fixture
def voice_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_mod, "VOICE_DIR", str(tmp_path))
    return tmp_path


# ---- shape ------------------------------------------------------------------------------------
def test_manual_red_response_shape():
    r = post(input_type="manual", batch_number="AX2291", user_language="en")
    assert r.status_code == 200
    j = r.json()
    assert TOP_KEYS <= j.keys()
    assert (j["verdict"], j["verdict_reason"], j["also_expired"], j["dispute_open"]) == ("RED", "nsq_match", False, False)
    assert set(j["safe_action"]) == {"text", "voice_note_url", "report_enabled"}
    assert j["safe_action"]["text"] == "Do not consume. Show this result to a pharmacist. Keep the packaging."
    assert j["safe_action"]["report_enabled"] is True
    assert j["served_from_cache"] is False and j["cache_last_updated"] is None
    assert j["evidence_matrix"] and all(ROW_KEYS <= row.keys() for row in j["evidence_matrix"])
    nsq = next(x for x in j["evidence_matrix"] if x["signal_type"] == "nsq_status")
    assert nsq["status"] == "FAIL" and nsq["official"] is True and nsq["source_reference"].endswith(".pdf") and nsq["synthetic"] is True
    assert j["demo_notice"] and "synthetic" in j["demo_notice"]


@pytest.mark.parametrize("batch,verdict,extra", [("EN3302", "EXPIRED", {}), ("MQ7756", "GREEN", {}), ("FM9184", "RED", {"also_expired": True}),
                                                 ("PT8813", "RED", {"dispute_open": True}), ("BQ7743", "AMBER", {})])
def test_manual_verdicts_and_flags(batch, verdict, extra):
    j = post(input_type="manual", batch_number=batch).json()
    assert j["verdict"] == verdict and all(j[k] == v for k, v in extra.items())


def test_lowercase_batch_is_normalised_and_language_selects_safe_action():
    en = post(input_type="manual", batch_number="mq7756").json()
    hi = post(input_type="manual", batch_number="MQ7756", user_language="hi").json()
    kn = post(input_type="manual", batch_number="MQ7756", user_language="kn").json()
    assert en["verdict"] == "GREEN" and en["safe_action"]["text"].startswith("No warnings found in our records.")
    assert hi["safe_action"]["text"].startswith("हमारे रिकॉर्ड") and kn["safe_action"]["text"].startswith("ನಮ್ಮ")


def test_unknown_batch_has_no_demo_notice():
    j = post(input_type="manual", batch_number="UNK0001").json()
    assert j["verdict"] == "GREEN" and j["demo_notice"] is None


# ---- voice note URL ---------------------------------------------------------------------------
def test_voice_note_url_null_when_file_missing_and_set_when_present(voice_dir):
    assert post(input_type="manual", batch_number="AX2291").json()["safe_action"]["voice_note_url"] is None
    (voice_dir / "RED_nsq_en.ogg").write_bytes(b"OggS")
    (voice_dir / "RED_nsq_hi.ogg").write_bytes(b"OggS")
    assert post(input_type="manual", batch_number="AX2291").json()["safe_action"]["voice_note_url"] == "/voice/RED_nsq_en.ogg"
    assert post(input_type="manual", batch_number="AX2291", user_language="hi").json()["safe_action"]["voice_note_url"] == "/voice/RED_nsq_hi.ogg"
    # kn file missing -> falls back to English audio rather than a broken link
    assert post(input_type="manual", batch_number="AX2291", user_language="kn").json()["safe_action"]["voice_note_url"] == "/voice/RED_nsq_en.ogg"


def test_voice_url_uses_escalated_variant_for_escalated_verdict(voice_dir, reg, monkeypatch):
    (voice_dir / "RED_escalated_en.ogg").write_bytes(b"OggS")
    flagged = engine.Signal("replay_pattern", "FLAGGED", "[Preview] x", "scan_log", "s", "low", synthetic=True)
    monkeypatch.setattr(engine, "replay_signal", lambda batch, serial=None: flagged)
    j = post(input_type="photo", photo_base64=b64(to_bytes(pack_for(reg, mfr="Zenith Labs Pvt. Ltd.")))).json()
    assert j["verdict_reason"] == "escalated_conflict" and j["escalated_from"] and j["safe_action"]["voice_note_url"] == "/voice/RED_escalated_en.ogg"


# ---- qr input ---------------------------------------------------------------------------------
def test_qr_input_valid_payload(reg):
    j = post(input_type="qr", qr_payload=gs1(reg["product_code"], reg["batch_number"], reg["exp_yymmdd"])).json()
    assert j["verdict"] == "GREEN" and j["notes"] == []
    assert {r["signal_type"] for r in j["evidence_matrix"]} == {"code_validity", "nsq_status", "expiry_status", "replay_pattern"}


def test_qr_input_raw_gs1_with_nsq_batch():
    j = post(input_type="qr", qr_payload=f"01{G}10AX2291\x1d17280331\x1d21SN1").json()
    assert j["verdict"] == "RED" and j["verdict_reason"] == "nsq_match"


def test_qr_input_garbage_payload_is_amber_qr_fail():
    j = post(input_type="qr", qr_payload="https://example.com").json()
    assert j["verdict"] == "AMBER" and j["notes"] == ["QR_FAIL"]


def test_qr_input_requires_payload():
    assert post(input_type="qr").status_code == 422


# ---- photo input ------------------------------------------------------------------------------
@pytest.mark.parametrize("batch,verdict", [("AX2291", "RED"), ("EN3302", "EXPIRED"), ("MQ7756", "GREEN"), ("GH6625", "AMBER"), ("CT5510", "RED")])
def test_photo_input_on_shipped_packs(batch, verdict):
    raw = open(os.path.join(PACKS, f"{batch}.png"), "rb").read()
    j = post(input_type="photo", photo_base64=b64(raw)).json()
    assert j["verdict"] == verdict and TOP_KEYS <= j.keys()
    assert "label_consistency" in {r["signal_type"] for r in j["evidence_matrix"]}


def test_photo_data_uri_prefix_accepted(reg):
    raw = to_bytes(pack_for(reg))
    r = post(input_type="photo", photo_base64="data:image/png;base64," + b64(raw))
    assert r.status_code == 200 and r.json()["verdict"] == "GREEN"


def test_photo_without_code_is_amber_with_qr_fail_note():
    j = post(input_type="photo", photo_base64=b64(to_bytes(Image.new("RGB", (900, 900), "white")))).json()
    assert j["verdict"] == "AMBER" and j["notes"][0] == "QR_FAIL"


def test_photo_quality_notes_are_returned(reg):
    from test_vision import blur
    j = post(input_type="photo", photo_base64=b64(to_bytes(blur(pack_for(reg), 2.5)))).json()
    assert j["notes"] == ["BLUR"] and j["verdict"] == "AMBER"


# ---- rejection --------------------------------------------------------------------------------
def test_oversize_photo_rejected_413():
    big = b64(b"\x89PNG" + b"0" * (10 * 1024 * 1024 + 10))
    assert post(input_type="photo", photo_base64=big).status_code == 413


def test_photo_just_over_limit_after_decode_rejected_413():
    raw = b"\x89PNG\r\n" + b"0" * (10 * 1024 * 1024 - 5)               # 10 MB + 1 byte once decoded, base64 length within the coarse cap
    assert post(input_type="photo", photo_base64=b64(raw)).status_code == 413


@pytest.mark.parametrize("payload", [
    "!!!not base64!!!", b64(b"plain text, not an image"), b64(b""), b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10),
    b64(to_bytes(Image.new("RGB", (40, 40)), "GIF")), b64(to_bytes(Image.new("RGB", (40, 40)), "BMP")), b64(b"%PDF-1.4"),
])
def test_invalid_or_unsupported_photo_rejected_422(payload):
    r = post(input_type="photo", photo_base64=payload)
    assert r.status_code == 422, r.text


def test_photo_required_for_photo_input():
    assert post(input_type="photo").status_code == 422
    assert post(input_type="photo", photo_base64="").status_code == 422


@pytest.mark.parametrize("body", [
    {}, {"input_type": "voice"}, {"input_type": "manual"}, {"input_type": "manual", "batch_number": ""},
    {"input_type": "manual", "batch_number": "AB"}, {"input_type": "manual", "batch_number": "A" * 21},
    {"input_type": "manual", "batch_number": "AX 2291"}, {"input_type": "manual", "batch_number": "AX2291", "user_language": "fr"},
    {"input_type": "manual", "batch_number": 12345},
])
def test_bad_requests_are_422(body):
    assert client.post("/api/verify", json=body).status_code == 422


@pytest.mark.parametrize("bad", ["AX2291'; DROP TABLE nsq_alerts;--", "' OR '1'='1", "Robert'); DROP TABLE batches;--", "AX2291\" OR 1=1 --",
                                 "%' UNION SELECT * FROM consent_log --", "AX2291\x00", "AX2291;", "../../etc/passwd"])
def test_sql_injection_shaped_batch_rejected_and_db_intact(bad):
    n = db.one("SELECT count(*) AS n FROM nsq_alerts")["n"]
    r = post(input_type="manual", batch_number=bad)
    assert r.status_code == 422
    assert db.one("SELECT count(*) AS n FROM nsq_alerts")["n"] == n
    assert db.one("SELECT to_regclass('batches') IS NOT NULL AS ok")["ok"]


def test_sql_injection_shaped_qr_payload_is_harmless():
    j = post(input_type="qr", qr_payload=f"(01){G}(10)AX2291'; DROP TABLE nsq_alerts;--(17)280331").json()
    assert j["verdict"] == "AMBER" and j["notes"] == ["QR_FAIL"]
    assert db.one("SELECT count(*) AS n FROM nsq_alerts")["n"] >= 200


def test_get_not_allowed():
    assert client.get("/api/verify").status_code == 405


# ---- degraded mode ------------------------------------------------------------------------------
def test_degraded_mode_served_from_cache(monkeypatch):
    snapshot.load_snapshot()
    down = lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("connection refused"))
    for fn in ("one", "all_", "run"):
        monkeypatch.setattr(db, fn, down)
    r = post(input_type="manual", batch_number="AX2291")
    assert r.status_code == 200
    j = r.json()
    assert j["verdict"] == "RED" and j["served_from_cache"] is True and j["cache_last_updated"] == date.today().isoformat()
    prefix = f"Data as of {date.today():%d %b %Y} — live records unavailable."
    assert j["safe_action"]["text"].startswith(prefix)
    assert next(x for x in j["evidence_matrix"] if x["signal_type"] == "nsq_status")["evidence_text"].startswith(prefix)
    assert j["safe_action"]["report_enabled"] is False
    g = post(input_type="manual", batch_number="MQ7756").json()
    assert g["verdict"] == "GREEN" and g["served_from_cache"] and g["safe_action"]["text"].startswith(prefix)


def test_degraded_mode_without_snapshot_is_amber_not_green_not_500(monkeypatch):
    down = lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("connection refused"))
    for fn in ("one", "all_", "run"):
        monkeypatch.setattr(db, fn, down)
    for body in ({"input_type": "manual", "batch_number": "MQ7756"}, {"input_type": "qr", "qr_payload": f"(01){G}(10)MQ7756(17)291201"}):
        r = post(**body)
        assert r.status_code == 200
        j = r.json()
        assert j["verdict"] == "AMBER" and j["verdict_reason"] == "data_unavailable" and j["served_from_cache"] is True and j["cache_last_updated"] is None


def test_degraded_photo_path_still_answers(monkeypatch):
    snapshot.load_snapshot()
    down = lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("connection refused"))
    for fn in ("one", "all_", "run"):
        monkeypatch.setattr(db, fn, down)
    raw = open(os.path.join(PACKS, "AX2291.png"), "rb").read()
    j = post(input_type="photo", photo_base64=b64(raw)).json()
    assert j["verdict"] == "RED" and j["served_from_cache"] is True


def test_healthy_db_after_outage_is_not_cached(monkeypatch):
    snapshot.load_snapshot()
    real = db.all_
    monkeypatch.setattr(db, "all_", lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError("down")))
    assert post(input_type="manual", batch_number="MQ7756").json()["served_from_cache"] is True
    monkeypatch.setattr(db, "all_", real)
    assert post(input_type="manual", batch_number="MQ7756").json()["served_from_cache"] is False
