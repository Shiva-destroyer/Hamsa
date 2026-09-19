-- Hamsa v2 -- demo fixups. IDEMPOTENT: re-run before every rehearsal and again 1 hour before the demo.
-- Run AFTER seed.sql and AFTER the 20260919* migrations (whatsapp_pivot, retention_and_ingest).
BEGIN;

-- A. Date drift. The seed's expiry dates were generated relative to 2026-09-18. Two "not expired"
--    scenario batches are already past expiry on/after 2026-09-19 (CT5510 expired 2026-09-18,
--    JK4471 expired 2026-07-23), which silently changes their verdicts. Pin them to the future
--    relative to whatever day you run this on.
UPDATE batches SET expiry_date = (CURRENT_DATE + INTERVAL '540 days')::date WHERE batch_number IN ('CT5510','JK4471');
-- EN3302 (EXPIRED demo) and FM9184 (RED + also expired) must stay expired: assert rather than assume.
UPDATE batches SET expiry_date = (CURRENT_DATE - INTERVAL '300 days')::date WHERE batch_number = 'EN3302' AND expiry_date >= CURRENT_DATE - INTERVAL '30 days';
UPDATE batches SET expiry_date = (CURRENT_DATE - INTERVAL '60 days')::date  WHERE batch_number = 'FM9184' AND expiry_date >= CURRENT_DATE - INTERVAL '30 days';
-- GREEN demo batch must stay in date.
UPDATE batches SET expiry_date = (CURRENT_DATE + INTERVAL '900 days')::date WHERE batch_number IN ('MQ7756','NS3340') AND expiry_date < CURRENT_DATE + INTERVAL '180 days';

-- B. Reset the live-dispute demo (beat 5): AX2291 must start with NO dispute so the judge can create one.
DELETE FROM disputes WHERE batch_number = 'AX2291' AND channel = 'whatsapp';
UPDATE batches SET dispute_status = 'none' WHERE batch_number = 'AX2291';
-- B2. A dispute that was upheld during a rehearsal may have flipped the NSQ row; restore it to 'active'.
UPDATE nsq_alerts SET status = 'active' WHERE batch_number = 'AX2291' AND status <> 'active';

-- C. Registered manufacturer domains for the dispute identity check (hackathon-grade).
--    Existing dispute domains are reused; Amrutha Drugs (owner of demo batch AX2291) is added so the
--    live dispute demo has a domain a presenter can type.
INSERT INTO manufacturer_domains (manufacturer, domain)
SELECT DISTINCT submitted_by, submitter_domain FROM disputes
ON CONFLICT (domain) DO NOTHING;
INSERT INTO manufacturer_domains (manufacturer, domain) VALUES
  ('Amrutha Drugs Ltd.', 'amruthadrugs.com')
ON CONFLICT (domain) DO NOTHING;

-- C2. Provenance fix: replay evidence comes from the scan log, not from the user's photo.
UPDATE scan_evidence SET source_type = 'scan_log' WHERE signal_type = 'replay_pattern' AND source_type = 'user_photo';

-- E. Serial-level replay demo: the escalation pack CT5510 (beat 4) carries serial SN31555740
--    (crc32('CT5510') % 10^8, exactly as make_demo_packs.py encodes it). Two scans of that serial in DIFFERENT states
--    within 24 h -> replay_signal(batch, serial) is FLAGGED at serial level. hashed_serial_id uses the same function as
--    replay.serial_hash(): first 32 hex chars of sha256(serial). Tagged 'serial_replay_demo'; origin='seed' so the
--    retention jobs never touch them; fixed ids + ON CONFLICT keep this idempotent (2 rows total).
--    Deliberately NOT done for the other packs: MQ7756 must stay GREEN and GH6625 AMBER-only.
INSERT INTO scan_events (id, hashed_serial_id, product_code, batch_number, approx_location, scanned_at, source, risk_level, risk_reasons, origin)
SELECT md5('serial_replay_demo:CT5510:' || n)::uuid,
       substr(encode(sha256(convert_to('SN31555740', 'UTF8')), 'hex'), 1, 32),
       b.product_code, 'CT5510', loc, ts, 'whatsapp', 'amber',
       ARRAY['replay_pattern_flagged_preview', 'serial_replay_demo'], 'seed'
FROM batches b,
     (VALUES (1, '110001 (New Delhi, Delhi)',          TIMESTAMPTZ '2026-09-10 08:15:00+00'),
             (2, '560034 (Bengaluru Urban, Karnataka)', TIMESTAMPTZ '2026-09-10 14:40:00+00')) AS v(n, loc, ts)
WHERE b.batch_number = 'CT5510'
ON CONFLICT (id) DO UPDATE SET hashed_serial_id = EXCLUDED.hashed_serial_id, approx_location = EXCLUDED.approx_location,
    scanned_at = EXCLUDED.scanned_at, risk_reasons = EXCLUDED.risk_reasons, origin = 'seed';

-- D. Wipe rate-limit / session state left over from rehearsals.
TRUNCATE wa_sessions, wa_inbound_dedupe;

COMMIT;
