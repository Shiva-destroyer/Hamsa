"""Simulates Meta delivering an inbound WhatsApp message to your local server (no phone, no tunnel needed).
Start the server with DRY_RUN=1 so replies print in the server console instead of calling Meta.

  python scripts/simulate_inbound.py --text hi
  python scripts/simulate_inbound.py --button agree
  python scripts/simulate_inbound.py --button lang_hi
  python scripts/simulate_inbound.py --text AX2291
  python scripts/simulate_inbound.py --image assets/packs/GH6625.png     (DRY_RUN only: path is read from disk)
  python scripts/simulate_inbound.py --text AX2291 --bad-signature        (must be rejected with 403)
"""
import argparse, hashlib, hmac, json, os, sys, time, uuid
import httpx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8000/webhook")
ap.add_argument("--from", dest="sender", default="919900000001")
ap.add_argument("--text")
ap.add_argument("--button")
ap.add_argument("--image")
ap.add_argument("--bad-signature", action="store_true", help="prove forged requests are rejected")
a = ap.parse_args()

msg = {"from": a.sender, "id": "wamid.SIM" + uuid.uuid4().hex[:20], "timestamp": str(int(time.time()))}
if a.text is not None:
    msg.update(type="text", text={"body": a.text})
elif a.button:
    msg.update(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": a.button, "title": a.button}})
elif a.image:
    msg.update(type="image", image={"id": "sim-media", "mime_type": "image/png", "caption": a.image})
else:
    sys.exit("give --text, --button or --image")

payload = {"object": "whatsapp_business_account", "entry": [{"id": "0", "changes": [{"field": "messages", "value": {
    "messaging_product": "whatsapp", "metadata": {"phone_number_id": config.WA_PHONE_ID or "0"}, "messages": [msg]}}]}]}
raw = json.dumps(payload).encode()
sig = hmac.new(config.WA_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
if a.bad_signature:
    sig = "0" * 64
r = httpx.post(a.url, content=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + sig})
print(r.status_code, r.text)
