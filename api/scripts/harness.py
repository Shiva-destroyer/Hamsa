"""In-process WhatsApp harness: no network, no phone, no tunnel. Drives flow.handle / the signed /webhook and captures every
message that would have gone to Meta. Used by rehearse.py, simulate_load.py, tests/test_e2e.py and tests/test_chaos.py.
Forces DRY_RUN=1 so a rehearsal can never message a real person."""
import hashlib, hmac, json, os, sys, uuid
from collections import defaultdict

os.environ["DRY_RUN"] = "1"; os.environ.setdefault("FLORENCE", "0")     # rehearsals are offline and deterministic (Tesseract path); FLORENCE=1 to rehearse with the model
os.environ.setdefault("WA_APP_SECRET", "testsecret"); os.environ.setdefault("WA_VERIFY_TOKEN", "vtoken")
os.environ.setdefault("IDENTITY_SECRET", "idsecret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, db, flow, wa

_BOX = defaultdict(list)          # recipient number -> outbound message bodies since that Chat last sent something
MEDIA = {}                        # media_id -> bytes served by the patched wa.download_media


def install(mp=None):
    """Capture outbound sends and serve inbound media from MEDIA. Pass pytest's `monkeypatch` so the patch is undone after the test
    (scripts patch for the life of the process)."""
    post, dl = (lambda body: _BOX[body["to"]].append(body) or {}), (lambda mid: MEDIA[mid])
    if mp: mp.setattr(wa, "_post", post); mp.setattr(wa, "download_media", dl)
    else: wa._post, wa.download_media = post, dl


class Chat:
    def __init__(self, sender=None, mp=None):
        install(mp)
        self.sender = sender or "9190" + str(uuid.uuid4().int)[:8]
        self.ph = wa.identity_hash(self.sender)

    @property
    def out(self): return _BOX[self.sender]
    @property
    def types(self): return [o["type"] for o in self.out]
    @property
    def said(self):
        return "\n".join(o["text"]["body"] if o["type"] == "text" else o["interactive"]["body"]["text"] for o in self.out if o["type"] in ("text", "interactive"))
    @property
    def voice(self): return next((o["audio"]["id"] for o in self.out if o["type"] == "audio"), None)

    def send(self, kind="text", body="", **kw):
        _BOX[self.sender].clear()
        flow.handle(wa.Inbound("wamid." + uuid.uuid4().hex, self.sender, kind, body, **kw))
        return self

    def text(self, s): return self.send("text", s)
    def button(self, i): return self.send("button", i)

    def photo(self, data: bytes):
        mid = "media-" + uuid.uuid4().hex[:8]; MEDIA[mid] = data
        return self.send("image", media_id=mid, mime="image/png")

    def cooldown_off(self):
        db.run("UPDATE wa_sessions SET last_lookup_at = NULL WHERE phone_hash = %s", (self.ph,)); return self

    def onboard(self, lang="en"):
        self.text("hi"); self.button("agree"); self.button("lang_" + lang); return self

    def session(self): return db.one("SELECT * FROM wa_sessions WHERE phone_hash = %s", (self.ph,))

    def wipe(self):
        for sql in ("DELETE FROM reports WHERE reporter_identity_hash = %s", "DELETE FROM disputes WHERE submitter_identity_hash = %s",
                    "DELETE FROM uploads WHERE identity_hash = %s", "DELETE FROM wa_sessions WHERE phone_hash = %s",
                    "DELETE FROM consent_log WHERE user_identifier_hash = %s"):
            db.run(sql, (self.ph,))
        db.run("UPDATE batches SET dispute_status = 'none' WHERE batch_number = 'AX2291' AND NOT EXISTS "
               "(SELECT 1 FROM disputes WHERE batch_number = 'AX2291' AND status = 'open')")
        _BOX.pop(self.sender, None)


def sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(config.WA_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def webhook_payload(*messages) -> bytes:
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}).encode()


def read(path: str) -> bytes:
    with open(path, "rb") as f: return f.read()
