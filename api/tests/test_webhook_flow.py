import hashlib, hmac, json, uuid
from fastapi.testclient import TestClient
import app as appmod, config, db, wa

client = TestClient(appmod.app)
OUT = []
wa._post = lambda body: OUT.append(body) or {}                        # capture instead of calling Meta

def post(msg, sender="919911111111", sign=True):
    msg = {"from": sender, "id": "wamid." + uuid.uuid4().hex, "timestamp": "1", **msg}
    raw = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [msg]}}]}]}).encode()
    sig = hmac.new(config.WA_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest() if sign else "0" * 64
    return client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": "sha256=" + sig, "Content-Type": "application/json"})

def text(s, **k): return post({"type": "text", "text": {"body": s}}, **k)
def button(i, **k): return post({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": i, "title": i}}}, **k)
def last_texts(): return "\n".join(o["text"]["body"] if o["type"] == "text" else o["interactive"]["body"]["text"] for o in OUT if o["type"] in ("text", "interactive"))
def reset(sender="919911111111"):
    ph = wa.identity_hash(sender); db.run("DELETE FROM wa_sessions WHERE phone_hash=%s", (ph,)); db.run("DELETE FROM consent_log WHERE user_identifier_hash=%s", (ph,)); OUT.clear(); return ph

def test_verification_handshake():
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vtoken", "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"}).status_code == 403

def test_forged_signature_rejected():
    assert text("AX2291", sign=False).status_code == 403

def test_full_journey():
    ph = reset()
    scans = lambda: db.one("SELECT count(*) n FROM scan_events WHERE source='whatsapp'")["n"]; before = scans()
    text("hi");                       assert "Do you agree" in last_texts()            # consent is the FIRST message
    OUT.clear(); text("AX2291");      assert "Do you agree" in last_texts()            # no processing before consent
    assert scans() == before
    OUT.clear(); button("agree");     assert any(o["type"] == "interactive" for o in OUT)   # language buttons
    assert db.one("SELECT count(*) n FROM consent_log WHERE user_identifier_hash=%s", (ph,))["n"] == 1
    OUT.clear(); button("lang_hi");   assert "बैच नंबर" in last_texts()
    OUT.clear(); text("AX2291")
    body = last_texts(); assert "लाल" in body and "CDSCO" in body and "डेमो डेटा" in body  # Hindi verdict + evidence matrix + demo tag
    OUT.clear(); text("EN3302");      assert "कृपया कुछ सेकंड" in last_texts()          # rate limit: 1 lookup / 10 s
    db.run("UPDATE wa_sessions SET last_lookup_at = now() - interval '11 seconds' WHERE phone_hash=%s", (ph,))
    OUT.clear(); text("EN3302");      assert "एक्सपायर" in last_texts()
    OUT.clear(); text("WHY");         assert "Provenance" in last_texts() and "Confidence:" in last_texts()
    button("report"); button("rt1"); text("packaging looked wrong")
    assert db.one("SELECT count(*) n FROM reports WHERE reporter_identity_hash=%s", (ph,))["n"] == 1
    OUT.clear(); text("DELETE"); text("YES")
    assert db.one("SELECT count(*) n FROM wa_sessions WHERE phone_hash=%s", (ph,))["n"] == 0
    db.run("DELETE FROM reports WHERE reporter_identity_hash=%s", (ph,))

def test_dispute_flow_adds_banner_without_changing_verdict():
    ph = reset(); db.run("DELETE FROM disputes WHERE batch_number='AX2291' AND channel='whatsapp'"); db.run("UPDATE batches SET dispute_status='none' WHERE batch_number='AX2291'")
    text("hi"); button("agree"); button("lang_en"); text("AX2291"); OUT.clear()
    button("dispute"); text("regulatory@nirmalabio.com"); assert "isn't a registered manufacturer" in last_texts()      # wrong company's domain rejected
    db.run("UPDATE wa_sessions SET last_lookup_at=NULL WHERE phone_hash=%s", (ph,))
    text("AX2291"); button("dispute"); text("qa@amruthadrugs.com"); OUT.clear(); text("Corrected Certificate of Analysis attached")
    assert "Dispute filed" in last_texts()
    db.run("UPDATE wa_sessions SET last_lookup_at=NULL WHERE phone_hash=%s", (ph,)); OUT.clear(); text("AX2291")
    b = last_texts(); assert "RED" in b and "Under review" in b                    # banner added, verdict NOT changed
    db.run("DELETE FROM disputes WHERE batch_number='AX2291' AND channel='whatsapp'"); db.run("UPDATE batches SET dispute_status='none' WHERE batch_number='AX2291'"); reset()
