"""rate limiting, regulator JWT auth, media storage. Own FastAPI app; no dependency on app.py wiring."""
import io
import time
import uuid

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import auth, db, ratelimit, storage
from routers import regulator

SECRET = "w4-test-jwt-secret-0123456789-abcdefghij"
USER, PW = "w4-test-regulator", "w4-test-shared-secret-value"
IDENT = "w4test" + uuid.uuid4().hex


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("REGULATOR_USERNAME", USER)
    monkeypatch.setenv("REGULATOR_SHARED_SECRET", PW)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    ratelimit.reset()
    yield
    ratelimit.reset()
    db.run("DELETE FROM uploads WHERE identity_hash = %s", (IDENT,))
    db.run("DELETE FROM audit_log WHERE action LIKE 'regulator:%%' AND (actor = %s OR actor = 'anonymous')", (USER,))


def make_app():
    app = FastAPI()

    @app.get("/limited", dependencies=[Depends(ratelimit.ip_limit)])
    def limited():
        return {"ok": True}

    app.include_router(regulator.router)
    return app


client = TestClient(make_app())


# ---------------- rate limiting ----------------
def test_check_contract(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(ratelimit, "_now", lambda: t[0])
    assert ratelimit.check("k", 2, 10) == (True, 0)
    assert ratelimit.check("k", 2, 10) == (True, 0)
    ok, retry = ratelimit.check("k", 2, 10)
    assert not ok and retry == 10
    t[0] += 4
    assert ratelimit.check("k", 2, 10) == (False, 6)
    t[0] += 6.1
    assert ratelimit.check("k", 2, 10)[0] is True
    assert ratelimit.check("other", 2, 10)[0] is True             # keys are independent


def test_31st_call_is_429_with_retry_after():
    h = {"CF-Connecting-IP": "203.0.113.10"}
    for _ in range(30):
        assert client.get("/limited", headers=h).status_code == 200
    r = client.get("/limited", headers=h)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert client.get("/limited", headers={"CF-Connecting-IP": "203.0.113.11"}).status_code == 200   # other IP unaffected


def test_falls_back_to_client_host_without_cf_header():
    for _ in range(30):
        assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 429
    # a forged header is a different key: proves the header (not the socket) is what is keyed when present
    assert client.get("/limited", headers={"CF-Connecting-IP": "203.0.113.12"}).status_code == 200


def test_daily_cap(monkeypatch):
    t = [5000.0]
    monkeypatch.setattr(ratelimit, "_now", lambda: t[0])
    h = {"CF-Connecting-IP": "203.0.113.20"}
    for i in range(200):
        if i and i % 30 == 0:
            t[0] += 61                                            # new minute window, same day
        assert client.get("/limited", headers=h).status_code == 200
    t[0] += 61
    r = client.get("/limited", headers=h)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 3600                   # the day cap, not the minute cap
    t[0] += 86400
    assert client.get("/limited", headers=h).status_code == 200


# ---------------- JWT auth ----------------
def login(secret=PW, user=USER):
    return client.post("/api/regulator/login", json={"username": user, "secret": secret})


def test_login_ok_and_token_is_15_minutes():
    r = login()
    assert r.status_code == 200
    claims = jwt.decode(r.json()["access_token"], SECRET, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 900 and r.json()["expires_in"] == 900
    assert client.get("/api/regulator/reports", headers={"Authorization": "Bearer " + r.json()["access_token"]}).status_code == 200


def test_bad_credentials_401():
    assert login(secret="wrong").status_code == 401
    assert login(user="someone-else").status_code == 401


def test_missing_expired_forged_tokens_rejected():
    url = "/api/regulator/reports"
    assert client.get(url).status_code == 401
    now = int(time.time())
    good = {"sub": USER, "role": "regulator", "iat": now, "exp": now + 600}
    assert client.get(url, headers={"Authorization": "Bearer garbage"}).status_code == 401
    expired = jwt.encode({**good, "iat": now - 2000, "exp": now - 1000}, SECRET, algorithm="HS256")
    assert client.get(url, headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    forged = jwt.encode(good, "attacker-key-attacker-key-attacker-key", algorithm="HS256")
    assert client.get(url, headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    unsigned = jwt.encode(good, None, algorithm="none")
    assert client.get(url, headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401
    no_exp = jwt.encode({"sub": USER, "role": "regulator", "iat": now}, SECRET, algorithm="HS256")
    assert client.get(url, headers={"Authorization": f"Bearer {no_exp}"}).status_code == 401
    wrong_role = jwt.encode({**good, "role": "user"}, SECRET, algorithm="HS256")
    assert client.get(url, headers={"Authorization": f"Bearer {wrong_role}"}).status_code == 403


def test_fails_closed_when_secrets_unset(monkeypatch):
    token = login().json()["access_token"]
    for k in ("JWT_SECRET", "REGULATOR_USERNAME", "REGULATOR_SHARED_SECRET"):
        monkeypatch.delenv(k)
    assert login().status_code == 503
    assert client.get("/api/regulator/reports", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    monkeypatch.setenv("REGULATOR_USERNAME", USER)
    monkeypatch.setenv("REGULATOR_SHARED_SECRET", "")             # empty counts as unset
    monkeypatch.setenv("JWT_SECRET", SECRET)
    assert not auth.check_credentials(USER, "")
    assert login(secret="").status_code == 503


def test_login_is_rate_limited():
    h = {"CF-Connecting-IP": "203.0.113.30"}
    codes = [client.post("/api/regulator/login", json={"username": USER, "secret": "x"}, headers=h).status_code for _ in range(31)]
    assert codes[:30] == [401] * 30 and codes[30] == 429


# ---------------- storage ----------------
def _jpeg_with_gps() -> bytes:
    im = Image.new("RGB", (64, 48), (200, 30, 30))
    ex = Image.Exif()
    ex[0x010F] = "SecretPhoneMaker"
    gps = ex.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (12.0, 58.0, 30.0), "E", (77.0, 35.0, 10.0)
    b = io.BytesIO()
    im.save(b, "JPEG", exif=ex)
    return b.getvalue()


def _stored_path(ref):
    rel = db.one("SELECT path FROM uploads WHERE id = %s", (ref,))["path"]
    return storage.upload_dir() / rel


def test_exif_gps_stripped_before_write_jpeg():
    raw = _jpeg_with_gps()
    assert Image.open(io.BytesIO(raw)).getexif().get_ifd(0x8825)  # fixture really carries GPS
    ref = storage.save_media(IDENT, "report", raw, "image/jpeg")
    data = _stored_path(ref).read_bytes()
    im = Image.open(io.BytesIO(data))
    assert im.size == (64, 48)
    assert not im.getexif().get_ifd(0x8825) and 0x010F not in im.getexif()
    assert b"Exif" not in data and b"SecretPhoneMaker" not in data


def test_png_text_metadata_stripped():
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo(); info.add_text("GPSLatitude", "12.97")
    b = io.BytesIO(); Image.new("RGBA", (10, 10), (1, 2, 3, 128)).save(b, "PNG", pnginfo=info)
    assert b"GPSLatitude" in b.getvalue()
    ref = storage.save_media(IDENT, "dispute", b.getvalue(), "image/png")
    assert b"GPSLatitude" not in _stored_path(ref).read_bytes()


def test_pdf_and_webp_accepted():
    assert storage.save_media(IDENT, "dispute", b"%PDF-1.4 hello", "application/pdf")
    b = io.BytesIO(); Image.new("RGB", (8, 8)).save(b, "WEBP")
    ref = storage.save_media(IDENT, "report", b.getvalue(), "image/webp")
    assert _stored_path(ref).suffix == ".webp"


def test_rejects_wrong_type_oversize_bad_kind_and_traversal():
    jpg = _jpeg_with_gps()
    for mime in ("image/gif", "text/html", "application/x-msdownload", "", "image/jpeg/../../x"):
        with pytest.raises(storage.MediaRejected):
            storage.save_media(IDENT, "report", jpg, mime)
    with pytest.raises(storage.MediaRejected):                    # declared png, actually jpeg
        storage.save_media(IDENT, "report", jpg, "image/png")
    with pytest.raises(storage.MediaRejected):
        storage.save_media(IDENT, "report", b"<html>not an image</html>", "image/jpeg")
    with pytest.raises(storage.MediaRejected):                    # 10 MB + 1
        storage.save_media(IDENT, "report", b"%PDF-" + b"0" * storage.MAX_BYTES, "application/pdf")
    with pytest.raises(storage.MediaRejected):
        storage.save_media(IDENT, "report", b"", "application/pdf")
    for kind in ("../../etc", "report/../..", "/tmp", "", "REPORT"):
        with pytest.raises(storage.MediaRejected):
            storage.save_media(IDENT, kind, jpg, "image/jpeg")
    assert db.one("SELECT count(*) AS n FROM uploads WHERE identity_hash = %s", (IDENT,))["n"] == 0
    assert not list(storage.upload_dir().rglob("*.*")) if storage.upload_dir().exists() else True


def test_paths_stay_inside_upload_dir_and_hostile_identity_is_harmless():
    ref = storage.save_media("../../evil", "report", _jpeg_with_gps(), "image/jpeg")
    p = _stored_path(ref).resolve()
    assert storage.upload_dir() in p.parents
    db.run("DELETE FROM uploads WHERE id = %s", (ref,))
    assert storage.delete_file("../../../../etc/passwd") is False  # never deletes outside UPLOAD_DIR
