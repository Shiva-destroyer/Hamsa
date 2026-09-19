-- Hamsa v2 (WhatsApp-first) -- additive migration. Safe to run once, after 20260918000000_hamsa_schema.sql
-- Does NOT alter or drop anything the original schema created (only ADDs).
BEGIN;

-- 1. WhatsApp conversation state (state machine) -------------------------
--    Keyed by HMAC(phone) -- the raw phone number is never persisted (DPDP data minimisation).
CREATE TABLE IF NOT EXISTS wa_sessions (
    phone_hash       text PRIMARY KEY,
    language         text CHECK (language IN ('en','hi','kn')),
    state            text NOT NULL DEFAULT 'NEW',   -- NEW|CONSENT|LANG|MENU|AWAIT_BATCH|AWAIT_EXPIRY|VERDICT|REPORT_TYPE|REPORT_DESC|DISPUTE_EMAIL|DISPUTE_EVIDENCE|DELETE_CONFIRM
    context          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"batch":"AX2291","last_scan_id":"..."}
    consented_at     timestamptz,
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_lookup_at   timestamptz                      -- powers 1 lookup / 10 s / number
);

-- 2. Webhook idempotency: Meta retries deliveries; the same wamid must never be processed twice ---
CREATE TABLE IF NOT EXISTS wa_inbound_dedupe (
    wamid        text PRIMARY KEY,
    received_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wa_dedupe_received ON wa_inbound_dedupe(received_at);

-- 3. Registered manufacturer email domains: the hackathon-grade dispute identity check
CREATE TABLE IF NOT EXISTS manufacturer_domains (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    manufacturer  text NOT NULL,
    domain        text NOT NULL UNIQUE
);

-- 4. Disputes now arrive over WhatsApp, not a web form ---------------------------------------
ALTER TABLE disputes ADD COLUMN IF NOT EXISTS submitter_identity_hash text;   -- HMAC of WhatsApp number
ALTER TABLE disputes ADD COLUMN IF NOT EXISTS channel channel_enum NOT NULL DEFAULT 'whatsapp';

-- 5. Data-origin flag: the demo must never present synthetic rows as genuine CDSCO records ------
ALTER TABLE nsq_alerts ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'synthetic'
    CHECK (data_origin IN ('synthetic','cdsco_real'));

-- 6. Fix ambiguity in seed: "Inconclusive" was used both for (a) "no scan-log infrastructure"
--    (default, NOT a signal) and (b) "impossible-travel pattern flagged" (a real AMBER signal).
--    The engine must be able to tell them apart or every scan would look like it has a replay flag.
UPDATE scan_evidence
   SET status = 'Flagged (preview)'
 WHERE signal_type = 'replay_pattern'
   AND evidence_text ILIKE '%same serial recorded%';

-- 6b. Schema gap: source_type_enum has no 'scan_log', so the seed labels replay evidence as 'user_photo'
--     (the Provenance panel would tell a user the replay flag came from THEIR photo). Add the value here;
--     demo_fixups.sql relabels the seed rows (a new enum value cannot be used in the same transaction).
ALTER TYPE source_type_enum ADD VALUE IF NOT EXISTS 'scan_log';

-- 7. Lock the tables down. Supabase exposes public-schema tables through PostgREST; with RLS off,
--    the public anon key could read consent_log / reports / disputes. The API uses the service role
--    (bypasses RLS), so "enable RLS + no policies" = deny-all for everyone else.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['products','manufacturer_aliases','batches','nsq_alerts','scan_events',
       'scan_evidence','reports','disputes','consent_log','audit_log','wa_sessions',
       'wa_inbound_dedupe','manufacturer_domains']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;

COMMIT;
