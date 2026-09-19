# Live WhatsApp deployment (tunnel method)

This is how the prototype is deployed: the API and PostgreSQL run on one machine; a Cloudflare quick tunnel exposes only `/webhook` to Meta.

    Phone (WhatsApp) → Meta WhatsApp Cloud API → Cloudflare Tunnel → FastAPI on localhost:8000 → PostgreSQL 16

It is a prototype deployment, not a production design: the tunnel URL changes on every restart, everything runs in one process, and a Meta test number accepts at most 5 allow-listed recipients.

## 1. One-time setup

1. Offline setup works first ([SETUP.md](SETUP.md)): `python api/scripts/rehearse.py` passes.
2. **Meta app.** developers.facebook.com → Create App → add the WhatsApp product → *API Setup*. Note the **Phone number ID** (not the phone number), the **App ID** and the **App secret**. Put them in `api/.env` in your editor — never paste secrets into a chat or commit them: `WA_PHONE_NUMBER_ID`, `WA_ACCESS_TOKEN`, `WA_APP_SECRET`, `WA_APP_ID`, plus your own random `WA_VERIFY_TOKEN`, `IDENTITY_SECRET`, `JWT_SECRET`, `REGULATOR_USERNAME` and `REGULATOR_SHARED_SECRET`. Set `DRY_RUN=0`.
3. **Allow-list phones.** API Setup → *To* → Manage phone number list. A test number accepts at most 5 recipients, and each number is permanent once added. Each phone should send "hi" to the test number to open its 24-hour window (the bot never messages first).
4. **Long-lived token.** The API Setup token lasts 24 hours. For a longer deployment create a System User token: business.facebook.com/settings → Users → System users → add a user → assign your app and WhatsApp account → generate a token with `whatsapp_business_messaging` and `whatsapp_business_management`, expiration *Never*, and put it in `WA_ACCESS_TOKEN`. `preflight.py` reports the token's expiry.

## 2. Start order

    docker compose up -d db
    python api/scripts/db_setup.py --db hamsa --fixups-only
    cd api && DRY_RUN=0 ENABLE_SCHEDULER=1 uvicorn app:app --port 8000
    cloudflared tunnel --url http://localhost:8000          # prints https://<random>.trycloudflare.com (new terminal)
    python api/scripts/set_webhook.py https://<random>.trycloudflare.com
    python api/scripts/preflight.py                         # expects ALL GOOD

If `set_webhook.py` is rejected, register the webhook by hand: Meta dashboard → WhatsApp → Configuration → Webhook → Edit — Callback URL `<tunnel url>/webhook`, Verify token = `WA_VERIFY_TOKEN` → Verify and save → subscribe to **messages**.

## 3. Live checklist

| Check | Evidence |
|---|---|
| "hi" from an allow-listed phone → consent message with two buttons | log line `event=inbound` |
| Consent → language → menu | |
| Batch number → verdict text, then a **native voice note with waveform**, then 3 buttons | outbound status 200 in the log |
| Photo of a printed sample pack → verdict | |
| Button taps: report, dispute, check another | |
| Forged POST to the tunnel URL is rejected | `python api/scripts/simulate_inbound.py --url <tunnel>/webhook --text hi --bad-signature` → **403** |
| Restart the tunnel and re-register | `set_webhook.py` with the new URL |

## 4. Troubleshooting

The API writes one JSON log line per event; phone numbers never appear (only an 8-character hash prefix).

| Symptom | Cause / fix |
|---|---|
| No reply at all | The tunnel died. Restart `cloudflared`, run `set_webhook.py <new url>`, then check `curl "<url>/webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=1"` returns `1`. |
| Log hint `131030` | Recipient is not in the allow-list (test numbers only). |
| Log hint `131047` | The 24-hour window is closed: the user must message first. |
| Log hint `131053` | Media upload problem: a voice note must be OGG/OPUS, mono, ≤ 512 KB. |
| Log hint `190` | Access token invalid or expired: regenerate it and update `.env`. |
| Photo won't decode | Type the batch number; re-shoot in daylight, whole pack in frame. |
| Database down | The bot answers from the in-memory NSQ copy with a "Data as of …" prefix; restart PostgreSQL. |
| AX2291 is not RED after trying the dispute flow | `python api/scripts/db_setup.py --db hamsa --fixups-only` (or `regulator_demo.py --reset`). |

Rate limit: one lookup per 10 seconds per number. Do not run unofficial WhatsApp libraries on the number you use with the Cloud API.
