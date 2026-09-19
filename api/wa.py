"""Thin client for the Meta WhatsApp Cloud API (the ONLY WhatsApp integration in Hamsa v2).
Outbound sends retry once on 5xx / timeout; Graph errors are decoded to friendly hints and logged (never raised, never
containing tokens or raw numbers -- see log.py)."""
import hashlib, hmac, json, os, time
from dataclasses import dataclass
from typing import Optional
import httpx
import config
import log

RETRY_DELAY_S = 0.5
MAX_MEDIA_BYTES = 10 * 1024 * 1024 + 1            # storage.MAX_BYTES + 1: anything bigger is refused while streaming

GRAPH_HINTS = {
    131030: "recipient not in allow-list: add the number under WhatsApp > API Setup > 'To' and verify it (test numbers only)",
    131047: "24-hour window closed: the user must message the bot first (re-engagement needs an approved template)",
    131053: "media upload error: voice notes must be OGG container, OPUS codec, mono, <=512 KB, MIME 'audio/ogg; codecs=opus'",
    190: "access token invalid or expired: regenerate the System User token and update .env (never paste it in chat)",
}

def decode_error(status: int, body) -> dict:
    """Graph error payload -> {"code", "hint", "message"}. Safe on any input."""
    err = (body.get("error") if isinstance(body, dict) else None) or {}
    code = err.get("code")
    try: code = int(code) if code is not None else None
    except (TypeError, ValueError): code = None
    if code is None and status == 401: code = 190
    hint = GRAPH_HINTS.get(code) or (f"Graph API error {code}" if code else f"HTTP {status}")
    return {"code": code, "hint": hint, "message": str(err.get("message", ""))[:200]}

# ---------- inbound ----------
def verify_signature(raw_body: bytes, header: Optional[str]) -> bool:
    """Meta signs every webhook POST: X-Hub-Signature-256 = 'sha256=' + HMAC_SHA256(app_secret, raw_body)."""
    if not header or not header.startswith("sha256=") or not config.WA_APP_SECRET:
        return False
    expected = hmac.new(config.WA_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])

@dataclass
class Inbound:
    wamid: str
    sender: str                 # E.164 digits, used ONLY to reply -- never stored, never logged
    kind: str                   # text | image | button | list | document | audio | sticker | location | contacts | other
    text: str = ""              # text body, or the id of the tapped button/list row
    media_id: str = ""
    mime: str = ""

IGNORED_TYPES = {"reaction"}    # an emoji reaction is not a request: no reply

def _as_list(x): return x if isinstance(x, list) else []

def parse_inbound(payload) -> list:
    """Meta payload -> [Inbound]. Anything malformed is skipped (never raises: the webhook must answer 200)."""
    out = []
    if not isinstance(payload, dict): return out
    for entry in _as_list(payload.get("entry")):
        for ch in _as_list(entry.get("changes") if isinstance(entry, dict) else None):
            val = ch.get("value") if isinstance(ch, dict) else None
            if not isinstance(val, dict): continue
            for m in _as_list(val.get("messages")):          # 'statuses' (delivery receipts) are ignored on purpose
                try:
                    i = _parse_one(m)
                except (KeyError, TypeError, AttributeError):
                    i = None
                if i: out.append(i)
    return out

def _parse_one(m: dict):
    k = m.get("type", "other")
    if k in IGNORED_TYPES: return None
    wid, frm = m["id"], m["from"]
    if k == "text":
        return Inbound(wid, frm, "text", m["text"]["body"].strip())
    if k in ("image", "document"):
        d = m[k]
        return Inbound(wid, frm, k, d.get("caption", "") or "", d["id"], d.get("mime_type", "") or "")
    if k == "interactive":
        i = m["interactive"]; r = i.get("button_reply") or i.get("list_reply") or {}
        return Inbound(wid, frm, "button", r.get("id", ""))
    if k == "button":
        return Inbound(wid, frm, "button", m["button"].get("payload", ""))
    return Inbound(wid, frm, k)                              # audio, sticker, location, contacts, video, unsupported ...

def identity_hash(phone: str) -> str:
    """Irreversible per-deployment identity (DPDP data minimisation). Same number -> same hash; hash -> number is infeasible."""
    return hmac.new(config.IDENTITY_SECRET.encode(), phone.encode(), hashlib.sha256).hexdigest()

# ---------- outbound ----------
_media_cache = {}

def _headers():
    return {"Authorization": f"Bearer {config.WA_ACCESS_TOKEN}"}

def _who(to: str) -> str:
    return identity_hash(to)[:8]

def _request(method: str, url: str, what: str, **kw):
    """One HTTP call, retried ONCE on a 5xx or a timeout/transport error. Returns the httpx.Response (any status) or None if
    both attempts failed at the transport level. Errors are logged with Graph hints; never raised."""
    last = None
    for attempt in (1, 2):
        try:
            r = httpx.request(method, url, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = None
            log.event("wa_http_error", what=what, attempt=attempt, error=type(e).__name__, level="warn")
        else:
            if r.status_code < 500:
                return r
            last = r
            log.event("wa_http_5xx", what=what, attempt=attempt, status=r.status_code, level="warn")
        if attempt == 1: time.sleep(RETRY_DELAY_S)
    return last

def _log_failure(what: str, r, to: str = ""):
    try: body = r.json()
    except Exception: body = {}
    d = decode_error(r.status_code, body)
    log.event("wa_send_failed", what=what, status=r.status_code, graph_code=d["code"], hint=d["hint"], graph_message=d["message"],
              who=_who(to) if to else "", level="error")

def _deliver(body: dict) -> dict:
    """POST one message to Graph. Never raises: failures are logged (with a decoded hint) and {} / the error body is returned."""
    r = _request("POST", f"{config.GRAPH}/{config.WA_PHONE_ID}/messages", "send", headers=_headers(), json=body, timeout=20)
    if r is None:
        log.event("wa_send_failed", what="send", status=0, hint="network/timeout after 1 retry", who=_who(body.get("to", "")), level="error")
        return {}
    try: data = r.json() if r.content else {}
    except ValueError: data = {}
    if r.status_code >= 300:
        _log_failure("send", r, body.get("to", ""))
    return data

def _post(body: dict):
    if config.DRY_RUN:
        kind = body.get("type"); who = f"<{_who(body.get('to', ''))}>"     # never print the raw number, even in dev
        if kind == "text":
            print(f"[DRY_RUN -> {who}] TEXT\n{body['text']['body']}\n")
        elif kind == "interactive":
            i = body["interactive"]; print(f"[DRY_RUN -> {who}] BUTTONS: {i['body']['text'][:80]!r} {[b['reply']['title'] for b in i['action']['buttons']]}\n")
        else:
            print(f"[DRY_RUN -> {who}] {str(kind).upper()} {json.dumps(body.get(kind), ensure_ascii=False)}\n")
        return {"dry_run": True}
    return _deliver(body)

def send_text(to: str, text: str):
    return _post({"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096], "preview_url": False}})

def send_buttons(to: str, body: str, buttons: list):
    """buttons = [(id, title<=20 chars), ...] max 3. body <= 1024 chars."""
    return _post({"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": {
        "type": "button", "body": {"text": body[:1024]},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons[:3]]}}})

def mark_as_read(wamid: str):
    """Optional read receipt (blue ticks). Off unless WA_MARK_AS_READ=1; failures are logged, never raised."""
    if os.environ.get("WA_MARK_AS_READ") != "1" or not wamid or config.DRY_RUN:
        return None
    r = _request("POST", f"{config.GRAPH}/{config.WA_PHONE_ID}/messages", "mark_read", headers=_headers(), timeout=10,
                 json={"messaging_product": "whatsapp", "status": "read", "message_id": wamid})
    if r is not None and r.status_code >= 300:
        _log_failure("mark_read", r)
    return None

def upload_media(path: str, mime: str) -> str:
    if path in _media_cache:
        return _media_cache[path]
    if config.DRY_RUN:
        return f"dry-media-{os.path.basename(path)}"
    with open(path, "rb") as f:
        data = f.read()
    r = _request("POST", f"{config.GRAPH}/{config.WA_PHONE_ID}/media", "upload_media", headers=_headers(), timeout=30,
                 data={"messaging_product": "whatsapp", "type": mime}, files={"file": (os.path.basename(path), data, mime)})
    if r is None:
        raise RuntimeError("media upload failed (network)")
    if r.status_code >= 300:
        _log_failure("upload_media", r)
        raise RuntimeError(f"media upload failed ({r.status_code})")
    _media_cache[path] = r.json()["id"]
    return _media_cache[path]

def send_voice_note(to: str, path: str):
    """Native voice note: needs audio/ogg + OPUS codec (<=512 KB for the play icon) and "voice": true.
    A missing file or a failed upload skips the voice message (logged); the text and buttons are unaffected."""
    if not os.path.exists(path):
        log.event("voice_skipped", reason="file_missing", file=os.path.basename(path), level="warn"); return None
    try:
        mid = upload_media(path, "audio/ogg; codecs=opus")
    except Exception as e:
        log.event("voice_skipped", reason="upload_failed", file=os.path.basename(path), error=type(e).__name__, level="warn"); return None
    return _post({"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": mid, "voice": True}})

class MediaTooLarge(ValueError):
    pass

def download_media(media_id: str) -> bytes:
    """Inbound photos/documents: GET /{media-id} -> temporary URL -> GET url (both need the bearer token).
    Refuses (MediaTooLarge) anything over 10 MB while streaming. Raises on network/HTTP failure (the flow replies politely)."""
    meta = _request("GET", f"{config.GRAPH}/{media_id}", "media_meta", headers=_headers(), timeout=20)
    if meta is None or meta.status_code >= 300:
        if meta is not None: _log_failure("media_meta", meta)
        raise RuntimeError("media lookup failed")
    info = meta.json(); url = info.get("url", "")
    if not url.startswith("https://"):
        raise RuntimeError("media url missing or not https")
    if int(info.get("file_size") or 0) > MAX_MEDIA_BYTES - 1:
        raise MediaTooLarge("file larger than 10 MB")
    for attempt in (1, 2):
        try:
            with httpx.stream("GET", url, headers=_headers(), timeout=30) as blob:
                if blob.status_code >= 500 and attempt == 1:
                    log.event("wa_http_5xx", what="media_get", attempt=1, status=blob.status_code, level="warn"); time.sleep(RETRY_DELAY_S); continue
                blob.raise_for_status()
                buf = bytearray()
                for chunk in blob.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_MEDIA_BYTES:
                        raise MediaTooLarge("file larger than 10 MB")
                return bytes(buf)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.event("wa_http_error", what="media_get", attempt=attempt, error=type(e).__name__, level="warn")
            if attempt == 2: raise
            time.sleep(RETRY_DELAY_S)
    raise RuntimeError("media download failed")
