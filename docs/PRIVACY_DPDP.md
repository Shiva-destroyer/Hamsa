# Privacy and the DPDP Act — what this prototype does

This is an engineering description, not legal advice, and the prototype has not been through a legal or DPO review. It maps behaviour to the principles of India's Digital Personal Data Protection Act, 2023 as a design intent.

## What is processed

| Data | Why | How it is held |
|---|---|---|
| Phone number | To reply on WhatsApp | Kept in memory only to address the reply. Stored **only** as `HMAC-SHA256(IDENTITY_SECRET, number)`. Never written to the database or logs in clear. |
| Batch number / photo you send | To check the batch | Batch string is used for the lookup and a `scan_event`. A photo is decoded in memory and discarded unless you attach it to a report or dispute. |
| Language, session state | To carry on the conversation | `wa_sessions` row keyed by the hash. Deleted on decline/delete; purged after 30 days idle. |
| Consent record | Proof of consent | `consent_log` row (hash, type, time, channel) created when you tap *I agree*. |
| Report / dispute + attached photo or document | To let a regulator review | `reports` / `disputes` rows (hash, text). Attachments stored under a server-generated name; images are re-encoded to remove EXIF/GPS **before** being written; validated type (JPEG/PNG/WebP/PDF) and ≤10 MB. |

Not collected: name, address, location, contacts, scan history per user.

## Principles → behaviour

- **Notice and consent first.** The first message anyone receives is the consent notice with *I agree* / *No thanks*. Until agreement no batch, photo, or command is processed (tested: the media download function is never called before consent). Declining deletes the session — "nothing was stored".
- **Data minimisation.** Hashed identity only; no free-text profile; photos not stored by default.
- **Purpose limitation.** Data is used to produce the verdict and, if you choose, to file a report/dispute.
- **Storage limitation.** Retention jobs (`ENABLE_SCHEDULER=1`): unattached uploads purged after 30 days; `wa_inbound_dedupe` after 7 days; sessions idle 30 days; approximate scan location scrubbed after 180 days (live rows only, never the synthetic seed). *Not built:* purging attached uploads 90 days after a case resolves.
- **Right to erasure.** Reply `DELETE`, then `YES`: session, consent record and photos not attached to a report/dispute are erased. Reports/disputes already filed remain with the regulator until resolved — the bot says so before you confirm.
- **Security safeguards.** Webhook accepts only correctly signed requests; rate limits; regulator API behind JWT with an audit log of every call; structured logs redact numbers, tokens and secrets; Row-Level Security enabled on every table.
- **Community reports are never public** and never change a verdict.

## Data flows outside India

Messages pass through **Meta** (WhatsApp Cloud API) and **Cloudflare** (the tunnel terminates TLS and can see payloads). Both are non-Indian processors. For any cloud database use a Mumbai region. A production deployment would need a data-processing agreement with each, and a review of cross-border transfer rules.

## Known gaps

Prototype-grade regulator and manufacturer authentication ([LIMITATIONS.md](LIMITATIONS.md)); no Data Protection Officer, grievance channel or breach-notification process; no verified-parental-consent flow (relevant if minors use it); consent text is available in English, Hindi and Kannada but the Hindi and Kannada versions were reviewed by the developers only, not by an independent native speaker; PDF metadata is not stripped from stored documents.
