"""Hamsa WhatsApp-first API. Run: uvicorn app:app --port 8000   (localhost only; Cloudflare Tunnel exposes /webhook)"""
import json, os, threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, Request, Response, HTTPException, Query, Depends
from fastapi.staticfiles import StaticFiles
import config, db, engine, flow, florence, wa, jobs, snapshot
from ratelimit import ip_limit
from routers import regulator, user, disputes, timeline, verify

@asynccontextmanager
async def lifespan(_app):
    snapshot.load_snapshot()                                 # degraded-mode cache
    jobs.start()                                             # no-op unless ENABLE_SCHEDULER=1
    threading.Thread(target=florence.load, daemon=True).start()   # warm the photo-text model so the first WhatsApp photo isn't slow (no-op if FLORENCE=0)
    yield
    jobs.stop()

_dev_docs = os.environ.get("DEV_DOCS") == "1"
app = FastAPI(title="Hamsa", lifespan=lifespan, docs_url="/docs" if _dev_docs else None,
              redoc_url=None, openapi_url="/openapi.json" if _dev_docs else None)
for _r in (regulator.router, user.router, disputes.router, timeline.router):
    app.include_router(_r)
app.include_router(verify.router, dependencies=[Depends(ip_limit)])
_voice_dir = os.path.join(os.path.dirname(__file__), "assets", "voice")
os.makedirs(_voice_dir, exist_ok=True)
app.mount("/voice", StaticFiles(directory=_voice_dir), name="voice")

@app.get("/health")
def health():
    db.one("SELECT 1"); return {"ok": True, "dry_run": config.DRY_RUN}

@app.get("/webhook")
def verify(hub_mode: str = Query(None, alias="hub.mode"), hub_verify_token: str = Query(None, alias="hub.verify_token"),
           hub_challenge: str = Query(None, alias="hub.challenge")):
    """Meta calls this once when you press 'Verify and save'. Echo the challenge as plain text if the token matches."""
    if hub_mode == "subscribe" and hub_verify_token and hub_verify_token == config.WA_VERIFY_TOKEN:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(403, "verify token mismatch")

@app.post("/webhook")
async def inbound(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if not wa.verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403, "bad signature")           # anything not signed by Meta is dropped
    try:
        payload = json.loads(raw)
    except ValueError:                                       # signed but not JSON: nothing to do, and a 5xx would only make Meta retry
        return {"status": "ignored"}
    for m in wa.parse_inbound(payload):
        bg.add_task(flow.handle, m)                          # answer 200 immediately; do the work after (Meta retries slow webhooks)
    return {"status": "ok"}

@app.get("/api/lookup-batch/{batch_number}")               # kept for curl / QA; the product itself is WhatsApp
def lookup(batch_number: str, _rl=Depends(ip_limit)):
    v, rec = engine.verify_manual(batch_number)
    return {"verdict": v.verdict, "verdict_reason": v.reason, "also_expired": v.also_expired, "dispute_open": v.dispute_open,
            "evidence_matrix": [s.__dict__ for s in v.signals]}
