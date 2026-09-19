"""Replay preview: synthetic scan-log check. Always a PREVIEW; INCONCLUSIVE = nothing to check (not a signal).

Serial-level: serial_hash = sha256(serial)[:32]; FLAGGED when scan_events holds >= 2 rows with that hash in
different states within 24 h. Batch-level fallback: the seed tag `impossible_travel_demo` on the batch.
"""
import hashlib
from typing import Optional

import db
from engine import Signal

SOURCE = "Synthetic scan-log dataset (preview only)"
# State = last comma-separated part inside the trailing "(District, State)" of approx_location.
_STATE_SQL = r"NULLIF(btrim(substring(approx_location from ',\s*([^,)]+)\)\s*$')), '')"


def serial_hash(serial: str) -> str:
    """The one hash function for serials (used by demo_fixups.sql too, as sha256 hex, first 32 chars)."""
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()[:32]


def _serial_flagged(serial: str) -> bool:
    row = db.one(f"""
        WITH e AS (SELECT scanned_at, {_STATE_SQL} AS state FROM scan_events WHERE hashed_serial_id = %s)
        SELECT 1 FROM e a JOIN e b ON a.state IS NOT NULL AND b.state IS NOT NULL AND a.state <> b.state
                                  AND b.scanned_at BETWEEN a.scanned_at AND a.scanned_at + interval '24 hours'
        LIMIT 1""", (serial_hash(serial),))
    return bool(row)


def _batch_flagged(batch: str) -> bool:
    return bool(db.one("SELECT 1 FROM scan_events WHERE batch_number = %s AND 'impossible_travel_demo' = ANY(risk_reasons) LIMIT 1",
                       (batch,)))


def _has_data(batch: str) -> bool:
    return bool(db.one("SELECT 1 FROM batches WHERE batch_number = %s UNION ALL "
                       "SELECT 1 FROM scan_events WHERE batch_number = %s LIMIT 1", (batch, batch)))


def replay_signal(batch: str, serial: Optional[str] = None) -> Signal:
    batch = (batch or "").strip().upper()
    serial = (serial or "").strip() or None
    if serial and _serial_flagged(serial):
        return Signal("replay_pattern", "FLAGGED",
                      "[Preview] This serial was scanned in two different states within 24 hours — synthetic demo data",
                      "scan_log", SOURCE, "low", synthetic=True)
    if _batch_flagged(batch):
        return Signal("replay_pattern", "FLAGGED",
                      "[Preview] Same serial seen in two locations >1,000 km apart within a day — synthetic demo data",
                      "scan_log", SOURCE, "low", synthetic=True)
    if _has_data(batch):
        return Signal("replay_pattern", "INCONCLUSIVE",
                      "[Preview] No pattern flagged — synthetic demo data only",
                      "scan_log", SOURCE, "low", synthetic=True)
    return Signal("replay_pattern", "INCONCLUSIVE",
                  "[Preview] Nothing to check — no scan-log data for this batch (synthetic demo data only)",
                  "scan_log", SOURCE, "low", synthetic=True)
