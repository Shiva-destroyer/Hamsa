-- Hamsa v2 -- retention + ingest migration. Run once, after 20260919000000_whatsapp_pivot.sql.
BEGIN;

-- 1. scan_events.origin: every row that exists before this migration is seed data ('seed');
--    rows written by the running app default to 'live'. Add nullable -> backfill -> set default.
ALTER TABLE scan_events ADD COLUMN IF NOT EXISTS origin text;
UPDATE scan_events SET origin = 'seed' WHERE origin IS NULL;
ALTER TABLE scan_events ALTER COLUMN origin SET DEFAULT 'live';
ALTER TABLE scan_events ALTER COLUMN origin SET NOT NULL;

-- 2. uploads: user-submitted photos/voice/evidence kept only for the retention window.
--    identity_hash is the HMAC of the WhatsApp number (never the raw number).
CREATE TABLE IF NOT EXISTS uploads (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_hash    text,
    kind             text,
    path             text,
    mime             text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    attached_to_type text,
    attached_to_id   uuid
);
CREATE INDEX IF NOT EXISTS idx_uploads_created_at ON uploads(created_at);
CREATE INDEX IF NOT EXISTS idx_uploads_identity_hash ON uploads(identity_hash);

-- 3. nsq_ingest_runs: one row per attempt to load NSQ data (real CDSCO ingestion).
CREATE TABLE IF NOT EXISTS nsq_ingest_runs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    source       text,
    rows_new     int NOT NULL DEFAULT 0,
    status       text,
    note         text
);

-- 4. Deny-all RLS (the API uses the owner/service role, which bypasses it).
ALTER TABLE uploads         ENABLE ROW LEVEL SECURITY;
ALTER TABLE nsq_ingest_runs ENABLE ROW LEVEL SECURITY;

COMMIT;
