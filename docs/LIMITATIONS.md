# Limitations (honest list)

Nothing here is hidden in the product: the bot itself says GREEN ≠ genuine, tags synthetic records, and labels the replay check a preview.

## What a verdict does and does not mean

- **GREEN means "no warning found in our records" — never "genuine".** A counterfeit that matches a real batch's printed data, or a batch CDSCO has not (yet) listed, will be GREEN.
- **Clone codes are a real weakness.** Hamsa reads what is printed. A cloned QR/DataMatrix that copies a genuine pack's code is not detectable by this system. Serialisation with a stateful verification service (track-and-trace) is the actual fix; the replay check is a *preview* of that idea on **synthetic** scan data.
- **NSQ ≠ counterfeit.** A CDSCO "Not of Standard Quality" listing means a tested sample failed a standard. It is a strong reason to stop and ask a pharmacist, not proof of fraud.
- **A RED can be wrong.** Batch numbers can be mistyped or reused, and OCR can misread. The bot shows its sources and confidence; the Safe Action always sends the user to a pharmacist. A manufacturer can dispute a record; a regulator can overturn it.
- Not a substitute for a pharmacist's or doctor's advice.

## Data

- **Real data volume and confidence.** The loader accepted 529 rows from CDSCO's April–June 2025 central and state NSQ PDFs, each passing a page-text guard (batch, product and manufacturer must appear in the source page). All are `extraction_confidence='medium'` until a human spot-checks a sample against the PDFs. Spurious-drug lists are not parsed (1–2 rows per month).
- **`alert_date` for real rows is the last day of the alert month** — the PDFs carry no per-row date.
- **Everything else is synthetic** (650 batches, 126 products, scan history, replay events) and is tagged 🧪 in the bot. Synthetic rows share batch-number style with real ones; do not read a synthetic batch as a statement about a real product.
- **Freshness:** ingestion is a script (`load_real_nsq.py`) and an ingest scheduler module; the scheduler is not wired into the app's startup, so nothing refreshes CDSCO data automatically unless run by hand. The retention jobs *are* wired (`ENABLE_SCHEDULER=1`).
- The degraded-mode snapshot is loaded at startup and by `load_snapshot()`; it is not refreshed after ingestion automatically.

## Photo path

- Blur, low-resolution and crop thresholds were tuned on **generated** images. Accuracy on real phone photos of printed paper is **unmeasured** until a live camera test; keep manual batch entry as the primary path.
- OCR compares printed manufacturer, batch and expiry to the code; low-confidence OCR is reported as "could not read", never guessed.
- The "Text I read" reply comes from Florence-2 (English only; no per-word confidence, so weak output is refused as a whole and Tesseract answers instead) or Tesseract (Hindi/Kannada need the language data). Both can misread — e.g. Florence read "Mfd." as "Mdf." in testing — so the reply says to check it against the pack. It is informational only and never feeds the verdict.
- Screens (moiré) and glossy packaging defeat QR decoding.

## Languages

- Hindi and Kannada strings were drafted with AI assistance and reviewed by the developers; an independent native-speaker review has not been done; voice notes are synthetic TTS (edge-tts) and must be regenerated after any text change.
- The dynamic **evidence lines** inside a verdict (e.g. "Listed in CDSCO NSQ alert for April 2025…") and the `WHY` provenance are English-only.

## Platform

- **Meta test number:** at most 5 allow-listed recipients (permanent once added). A registered business number and display-name approval would remove the cap; timing unverified.
- **24-hour window:** the bot only replies to users; it never initiates (no paid templates).
- **From 1 Oct 2026** Meta bills service messages beyond 1,000 free per number per month (about 330 verdicts, three messages each).
- Meta reportedly restricts open-ended AI assistants on the platform; Hamsa is deliberately rule-based (no LLM in the loop).
- Unofficial WhatsApp libraries carry ban risk and are not used.
- **Everything runs on one laptop** behind a Cloudflare quick tunnel (random URL that changes on every restart; "not production"; Cloudflare terminates TLS). One uvicorn worker, in-memory rate limiter and per-phone locks: not horizontally scalable.

## Security and identity (prototype-grade)

- **Regulator auth** is a shared secret exchanged for a 15-minute JWT. Real deployment needs SSO/MFA and per-user accounts.
- **Manufacturer identity** for disputes is a company-email *domain* match against a prototype table, not GST / drug-licence verification, and the email is typed, not verified by sending a code.
- **Per-IP limits** trust `CF-Connecting-IP`, which is only safe because the API listens on localhost and is reached only through the tunnel. Exposing uvicorn directly would let clients spoof it.
- Uploaded **PDFs are stored byte-for-byte** (images are re-encoded and EXIF-stripped; PDF metadata is not stripped).
- The "purge attached uploads 90 days after a case resolves" job is not built; unattached uploads are purged after 30 days.
- `DELETE /api/user/data` requires the caller to supply the 64-hex identity hash header; in WhatsApp, `DELETE` + `YES` is the user path.
- Reports already filed survive `DELETE` until the regulator resolves them (the bot says so).

## Not built (by design)

Web dashboard · per-user scan history · location opt-in · pharmacy-side disputes · silent re-verification push · cross-manufacturer pattern screen · payments · free-form chat.
