-- Hamsa synthetic dataset -- schema migration
-- Data model for Hamsa (see docs/Hamsa_PRD_v1.pdf, Section 15).
-- ALL DATA IN THIS DATASET IS SYNTHETIC. See README.md.

BEGIN;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid(), optional

CREATE TABLE products (
    id                          uuid PRIMARY KEY,
    product_code                text UNIQUE NOT NULL,   -- GTIN
    brand_name                  text NOT NULL,
    generic_name                text NOT NULL,
    manufacturer                text NOT NULL,           -- canonical name
    manufacturer_alias_group_id uuid NOT NULL,
    licence_number               text NOT NULL,
    created_at                  timestamptz NOT NULL
);

CREATE TABLE manufacturer_aliases (
    id             uuid PRIMARY KEY,
    alias_group_id uuid NOT NULL,
    alias_text     text NOT NULL,
    is_canonical   boolean NOT NULL DEFAULT false
);
CREATE INDEX idx_manufacturer_aliases_group ON manufacturer_aliases(alias_group_id);

CREATE TYPE dispute_status_enum AS ENUM ('none', 'open', 'resolved_upheld', 'resolved_overturned');

CREATE TABLE batches (
    id                uuid PRIMARY KEY,
    product_code      text NOT NULL REFERENCES products(product_code),
    batch_number      text NOT NULL UNIQUE,
    manufacturing_date date NOT NULL,
    expiry_date       date NOT NULL,
    manufacturer      text NOT NULL,
    dispute_status    dispute_status_enum NOT NULL DEFAULT 'none'
);
-- batch_number already indexed via its UNIQUE constraint above

CREATE TYPE extraction_confidence_enum AS ENUM ('high', 'medium', 'low');
CREATE TYPE lab_type_enum AS ENUM ('central', 'state');
CREATE TYPE nsq_status_enum AS ENUM ('active', 'corrected', 'superseded');

CREATE TABLE nsq_alerts (
    id                    uuid PRIMARY KEY,
    product_name          text NOT NULL,
    batch_number          text NOT NULL,
    manufacturer          text NOT NULL,
    alert_date            date NOT NULL,
    reason                text NOT NULL,
    source_document       text NOT NULL,
    extraction_confidence extraction_confidence_enum NOT NULL,
    lab_type              lab_type_enum NOT NULL,
    status                nsq_status_enum NOT NULL DEFAULT 'active'
);
CREATE INDEX idx_nsq_alerts_batch_number ON nsq_alerts(batch_number);

CREATE TYPE scan_source_enum AS ENUM ('web', 'whatsapp');
CREATE TYPE risk_level_enum AS ENUM ('red', 'amber', 'expired', 'green');

-- scan_events: PREVIEW / SYNTHETIC DATA ONLY (see docs/Hamsa_PRD_v1.pdf, Sections 8 and 24).
CREATE TABLE scan_events (
    id               uuid PRIMARY KEY,
    hashed_serial_id text NOT NULL,       -- hashed; never the raw code
    product_code     text NOT NULL,
    batch_number     text NOT NULL,
    approx_location  text NOT NULL,       -- pincode/district granularity only
    scanned_at       timestamptz NOT NULL,
    source           scan_source_enum NOT NULL,
    risk_level       risk_level_enum NOT NULL,
    risk_reasons     text[] NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_scan_events_batch_number ON scan_events(batch_number);
CREATE INDEX idx_scan_events_scanned_at ON scan_events(scanned_at);

CREATE TYPE signal_type_enum AS ENUM (
    'code_validity', 'label_consistency', 'nsq_status', 'expiry_status',
    'replay_pattern', 'visual_heuristic', 'community_reports'
);
CREATE TYPE source_type_enum AS ENUM (
    'government_record', 'decoded_code', 'ocr_reading', 'user_photo', 'community_report'
);
CREATE TYPE evidence_confidence_enum AS ENUM ('high', 'medium', 'low', 'structured');

CREATE TABLE scan_evidence (
    id               uuid PRIMARY KEY,
    scan_id          uuid NOT NULL REFERENCES scan_events(id) ON DELETE CASCADE,
    signal_type      signal_type_enum NOT NULL,
    status           text NOT NULL,        -- Pass / FAIL / Inconclusive / EXPIRED / 'None (regulator view only)'
    evidence_text    text NOT NULL,
    source_type      source_type_enum NOT NULL,
    source_reference text NOT NULL,
    confidence       evidence_confidence_enum NOT NULL,
    created_at       timestamptz NOT NULL
);
CREATE INDEX idx_scan_evidence_scan_id ON scan_evidence(scan_id);

CREATE TYPE report_status_enum AS ENUM ('open', 'under_investigation', 'confirmed', 'dismissed');
CREATE TYPE report_visibility_enum AS ENUM ('regulator_only');

CREATE TABLE reports (
    id                     uuid PRIMARY KEY,
    scan_id                uuid NOT NULL REFERENCES scan_events(id),
    image_reference        text,
    report_type            text NOT NULL,
    description            text,
    reporter_identity_hash text NOT NULL,   -- hashed; never stored raw
    created_at             timestamptz NOT NULL,
    status                 report_status_enum NOT NULL DEFAULT 'open',
    visibility              report_visibility_enum NOT NULL DEFAULT 'regulator_only'
);
CREATE INDEX idx_reports_status ON reports(status);

CREATE TYPE dispute_full_status_enum AS ENUM ('open', 'under_review', 'resolved_upheld', 'resolved_overturned');

CREATE TABLE disputes (
    id                  uuid PRIMARY KEY,
    batch_number        text NOT NULL REFERENCES batches(batch_number),
    submitted_by        text NOT NULL,
    submitter_domain    text NOT NULL,
    evidence_reference  text,
    status              dispute_full_status_enum NOT NULL DEFAULT 'open',
    submitted_at        timestamptz NOT NULL,
    resolved_at         timestamptz,
    resolution_notes    text
);
CREATE INDEX idx_disputes_batch_number ON disputes(batch_number);

CREATE TYPE consent_type_enum AS ENUM ('data_processing', 'scan_history_opt_in', 'location_opt_in');
CREATE TYPE channel_enum AS ENUM ('web', 'whatsapp');

CREATE TABLE consent_log (
    id                  uuid PRIMARY KEY,
    user_identifier_hash text NOT NULL,
    consent_type        consent_type_enum NOT NULL,
    granted_at           timestamptz NOT NULL,
    channel              channel_enum NOT NULL
);
CREATE INDEX idx_consent_log_user ON consent_log(user_identifier_hash);

CREATE TABLE audit_log (
    id        uuid PRIMARY KEY,
    actor     text NOT NULL,
    action    text NOT NULL,
    target    text NOT NULL,
    "timestamp" timestamptz NOT NULL
);
CREATE INDEX idx_audit_log_actor ON audit_log(actor);

COMMIT;
