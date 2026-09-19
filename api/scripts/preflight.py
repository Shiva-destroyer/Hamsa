"""One command that tells you whether the demo will work.  Run it 1 hour before, then again 10 minutes before:
    python scripts/preflight.py
    python scripts/preflight.py --local     # skip the checks that need Meta / the tunnel / a running server (dev machines, CI)
Exit code 0 = every check passed.  Anything marked FAIL has a fix hint next to it."""
import json, os, re, sys, tempfile, time
import httpx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, db, engine, snapshot
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_demo_packs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = []
LOCAL = "--local" in sys.argv


def check(name, fn, hint="", network=False):
    if network and LOCAL:
        print(f"SKIP  {name:<46} (--local)"); return
    try:
        ok, detail = fn()
    except Exception as e:                                   # a crashing check is a failing check
        ok, detail = False, repr(e)[:160]
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name:<46} {detail}" + (f"\n      -> {hint}" if not ok and hint else ""))


def env_ok():
    miss = [k for k in ("WA_ACCESS_TOKEN", "WA_PHONE_NUMBER_ID", "WA_APP_SECRET", "WA_VERIFY_TOKEN", "WA_APP_ID", "PUBLIC_BASE_URL") if not os.getenv(k)]
    return (not miss), ("all set" if not miss else "missing: " + ", ".join(miss))


def db_ok():
    n = db.one("SELECT (SELECT count(*) FROM batches) b, (SELECT count(*) FROM nsq_alerts) n")
    return n["b"] >= 600 and n["n"] >= 190, f"batches={n['b']} nsq_alerts={n['n']}"


def verdicts_ok():
    want = {"AX2291": "RED", "EN3302": "EXPIRED", "MQ7756": "GREEN", "PT8813": "RED", "FM9184": "RED"}
    bad = [f"{b}={engine.verify_manual(b)[0].verdict} (want {w})" for b, w in want.items() if engine.verify_manual(b)[0].verdict != w]
    return not bad, "manual-entry demo batches give expected verdicts" if not bad else "; ".join(bad)


def dispute_reset_ok():
    r = db.one("SELECT count(*) n FROM disputes WHERE batch_number='AX2291' AND channel='whatsapp'")
    return r["n"] == 0, "AX2291 has no leftover WhatsApp dispute" if r["n"] == 0 else f"{r['n']} leftover dispute(s)"


def packs_ok():
    paths = {n: f"{ROOT}/assets/packs/{n}.png" for n in make_demo_packs.PACKS}
    missing = [n for n, p in paths.items() if not os.path.exists(p)]
    if missing: return False, "png missing: " + ", ".join(missing)
    bad = make_demo_packs.verify(paths, quiet=True)
    return not bad, f"{len(paths)} demo packs decode to their expected verdicts" if not bad else "wrong: " + ", ".join(bad)


def voice_ok():
    keys = ["RED_nsq", "RED_escalated", "EXPIRED", "AMBER", "GREEN"]
    missing, big = [], []
    for k in keys:
        for lang in ("en", "hi", "kn"):
            p = f"{ROOT}/assets/voice/{k}_{lang}.ogg"
            if not os.path.exists(p): missing.append(f"{k}_{lang}")
            elif os.path.getsize(p) > 512 * 1024: big.append(f"{k}_{lang}")
    return not (missing or big), "15 voice notes present, all < 512 KB" if not (missing or big) else f"missing={missing} too_big={big}"


def voice_manifest_ok():
    m = json.load(open(f"{ROOT}/assets/voice/MANIFEST.json", encoding="utf-8"))
    bad = [f"{e['key']}_{e['lang']}" for e in m
           if not os.path.exists(f"{ROOT}/assets/voice/{e['key']}_{e['lang']}.ogg")
           or os.path.getsize(f"{ROOT}/assets/voice/{e['key']}_{e['lang']}.ogg") != e["bytes"] or not 2 <= e["duration_s"] <= 20]
    return len(m) == 15 and not bad, f"MANIFEST.json: {len(m)} entries, all files present and sizes match" if not bad and len(m) == 15 else f"entries={len(m)} bad={bad}"


def snapshot_ok():
    snapshot.load_snapshot()
    d = snapshot.get_snapshot_date()
    return d is not None and not snapshot.is_degraded(), f"NSQ snapshot loaded ({d})" if d else "no snapshot in memory"


def scheduler_ok():
    on = os.getenv("ENABLE_SCHEDULER") == "1"
    return on, "ENABLE_SCHEDULER=1 (retention jobs run)" if on else "ENABLE_SCHEDULER is not 1 -- retention jobs will not run"


def real_batch_ok():
    b = os.getenv("DEMO_REAL_BATCH", "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9\-/]{3,19}", b):
        return True, "DEMO_REAL_BATCH not set -- beat 1 uses the synthetic AX2291 fallback"
    v = engine.verify_manual(b)[0].verdict
    return v == "RED", f"{b} -> {v}"


def uploads_ok():
    import storage
    d = storage.upload_dir(); d.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=d) as f: f.write(b"x")
    return True, f"{d} is writable"


def local_ok():
    r = httpx.get("http://localhost:8000/health", timeout=5)
    return r.status_code == 200 and not r.json().get("dry_run"), f"HTTP {r.status_code} dry_run={r.json().get('dry_run')}"


def tunnel_ok():
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    r = httpx.get(f"{base}/webhook", params={"hub.mode": "subscribe", "hub.verify_token": config.WA_VERIFY_TOKEN, "hub.challenge": "ping123"}, timeout=10)
    return r.status_code == 200 and r.text == "ping123", f"{base} -> HTTP {r.status_code} (round trip {r.elapsed.total_seconds():.1f}s)"


def token_ok():
    r = httpx.get(f"{config.GRAPH}/{config.WA_PHONE_ID}", params={"fields": "display_phone_number,verified_name,quality_rating"},
                  headers={"Authorization": f"Bearer {config.WA_ACCESS_TOKEN}"}, timeout=10)
    return r.status_code == 200, (r.json().get("display_phone_number", "") + " / " + r.json().get("verified_name", "")) if r.status_code == 200 else r.text[:120]


def token_never_expires():
    app_id = os.getenv("WA_APP_ID")
    r = httpx.get(f"{config.GRAPH}/debug_token", params={"input_token": config.WA_ACCESS_TOKEN, "access_token": f"{app_id}|{config.WA_APP_SECRET}"}, timeout=10)
    d = r.json().get("data", {})
    exp = d.get("expires_at", -1)
    left = "never" if exp == 0 else f"{(exp - time.time()) / 3600:.1f} h left"
    return exp == 0 or (exp - time.time()) > 48 * 3600, f"token expiry: {left}"


check("env vars present", env_ok, "fill .env from .env.example", network=True)
check("database reachable + seeded", db_ok, "psql -f supabase/migrations/*.sql then seed.sql, patch, demo_fixups.sql")
check("manual-entry demo verdicts", verdicts_ok, "re-run supabase/demo_fixups.sql")
check("no leftover dispute on AX2291", dispute_reset_ok, "re-run supabase/demo_fixups.sql")
check("photo demo packs (QR + OCR)", packs_ok, "python scripts/make_demo_packs.py (after demo_fixups.sql)")
check("voice notes pre-generated", voice_ok, "python scripts/gen_voice.py")
check("voice MANIFEST.json matches the files", voice_manifest_ok, "python scripts/gen_voice.py")
check("NSQ snapshot loaded (degraded-mode cache)", snapshot_ok, "restart the API / python -c 'import snapshot; snapshot.load_snapshot()'")
check("retention scheduler enabled", scheduler_ok, "set ENABLE_SCHEDULER=1 in .env and restart")
check("DEMO_REAL_BATCH is RED", real_batch_ok, "python scripts/load_real_nsq.py --pick-demo, then set DEMO_REAL_BATCH")
check("uploads directory writable", uploads_ok, "check UPLOAD_DIR permissions")
check("local server up (DRY_RUN off)", local_ok, "DRY_RUN=0 uvicorn app:app --port 8000", network=True)
check("tunnel reaches /webhook", tunnel_ok, "cloudflared tunnel --url http://localhost:8000 ; update PUBLIC_BASE_URL + python scripts/set_webhook.py <url>", network=True)
check("Meta access token valid", token_ok, "regenerate token; check WA_PHONE_NUMBER_ID is the ID, not the phone number", network=True)
check("token will not expire mid-demo", token_never_expires, "create a System User token with expiry = Never", network=True)
print("\nALL GOOD" if all(results) else f"\n{results.count(False)} CHECK(S) FAILED")
sys.exit(0 if all(results) else 1)
