-- Normalised batch key (upper-case, letters/digits only) so "ax-2291", "AX 2291" and "AX2291" hit one index entry.
-- Not UNIQUE: batches.batch_number stays the unique, exact identifier; verdict lookups still use it.
ALTER TABLE batches    ADD COLUMN IF NOT EXISTS batch_key text GENERATED ALWAYS AS (upper(regexp_replace(batch_number, '[^A-Za-z0-9]', '', 'g'))) STORED;
ALTER TABLE nsq_alerts ADD COLUMN IF NOT EXISTS batch_key text GENERATED ALWAYS AS (upper(regexp_replace(batch_number, '[^A-Za-z0-9]', '', 'g'))) STORED;
CREATE INDEX IF NOT EXISTS idx_batches_batch_key    ON batches(batch_key);
CREATE INDEX IF NOT EXISTS idx_nsq_alerts_batch_key ON nsq_alerts(batch_key);
