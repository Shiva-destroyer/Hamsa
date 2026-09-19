"""Points Meta's webhook at your CURRENT tunnel URL from the command line, so you don't have to click through the
dashboard every time cloudflared prints a new random URL.

Your server AND the tunnel must already be running: Meta immediately calls GET /webhook to verify.
    python scripts/set_webhook.py https://random-words.trycloudflare.com

If Meta ever rejects this call, do it by hand instead:
    App Dashboard -> WhatsApp -> Configuration -> Webhook -> Edit  (callback URL = <tunnel>/webhook)
"""
import os, sys
import httpx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

base = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("PUBLIC_BASE_URL", "")).rstrip("/")
app_id = os.getenv("WA_APP_ID")
assert base and app_id and config.WA_APP_SECRET, "need the tunnel URL as an argument, plus WA_APP_ID and WA_APP_SECRET in .env"
r = httpx.post(f"{config.GRAPH}/{app_id}/subscriptions", data={
    "object": "whatsapp_business_account", "callback_url": base + "/webhook",
    "verify_token": config.WA_VERIFY_TOKEN, "fields": "messages",
    "access_token": f"{app_id}|{config.WA_APP_SECRET}"}, timeout=30)
print(r.status_code, r.text)
