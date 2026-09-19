"""Hamsa Evidence Engine. Pure logic + thin DB lookups.
Never returns a bare number: every verdict carries the signals (the Evidence Matrix) behind it."""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from rapidfuzz import fuzz
import db
import snapshot

RED, AMBER, EXPIRED, GREEN = "RED", "AMBER", "EXPIRED", "GREEN"

@dataclass
class Signal:
    signal_type: str            # nsq_status | expiry_status | label_consistency | code_validity | replay_pattern | visual_heuristic
    status: str                 # PASS | FAIL | EXPIRED | INCONCLUSIVE | FLAGGED
    evidence_text: str
    source_type: str            # government_record | decoded_code | ocr_reading | user_photo | scan_log
    source_reference: str
    confidence: str             # high | medium | low | structured
    last_updated: str = ""
    official: bool = False      # only the CDSCO record is "official"
    synthetic: bool = False     # True if the underlying row is demo data
    dispute: str = "none"       # none | open | resolved
    from_cache: bool = False    # True when answered from the in-memory snapshot (degraded mode)

@dataclass
class Verdict:
    verdict: str
    reason: str                 # nsq_match | escalated_conflict | single_signal | expired | no_warning
    also_expired: bool
    dispute_open: bool
    signals: list = field(default_factory=list)
    secondary_warnings: int = 0          # AMBER signals that co-exist with an EXPIRED primary verdict
    escalated_from: list = field(default_factory=list)

# ---- Conflict Resolution ---------------------------------------------------------
NON_OFFICIAL = {"label_consistency", "code_validity", "visual_heuristic", "replay_pattern"}
# Photo quality is causally UPSTREAM of decoding and OCR: a blurry photo can produce a malformed payload
# and a bad label read. So it is never the "second, independent" signal next to those two (v2 clarification of rule 3).
UPSTREAM_OF = {"visual_heuristic": {"code_validity", "label_consistency"}}

def _independent(a: str, b: str) -> bool:
    if a == b:
        return False
    if b in UPSTREAM_OF.get(a, set()) or a in UPSTREAM_OF.get(b, set()):
        return False
    return True

def resolve_conflicts(signals: list, dispute_open: bool = False) -> Verdict:
    nsq_fail  = any(s.signal_type == "nsq_status" and s.status == "FAIL" for s in signals)
    expired   = any(s.signal_type == "expiry_status" and s.status == "EXPIRED" for s in signals)
    warn_types = sorted({s.signal_type for s in signals
                         if s.signal_type in NON_OFFICIAL and s.status in ("FAIL", "FLAGGED")})
    # rule 7: an open dispute pauses NEW escalations built on the disputed signal (the NSQ row);
    # it never changes an existing official verdict, it only adds a banner.
    if nsq_fail:                                                            # rule 1: official evidence always wins
        return Verdict(RED, "nsq_match", expired, dispute_open, signals)
    indep_pair = next(((a, b) for i, a in enumerate(warn_types) for b in warn_types[i + 1:] if _independent(a, b)), None)
    if indep_pair:                                                          # rule 3: two independent non-official -> RED
        return Verdict(RED, "escalated_conflict", expired, dispute_open, signals, escalated_from=list(indep_pair))
    if expired:                                                             # rule 6: expiry is independent of counterfeit status
        return Verdict(EXPIRED, "expired", True, dispute_open, signals, secondary_warnings=len(warn_types))
    if warn_types:                                                          # rule 2: one non-official signal caps at AMBER
        return Verdict(AMBER, "single_signal", False, dispute_open, signals)
    if any(s.signal_type == "nsq_status" and s.status == "INCONCLUSIVE" for s in signals):
        return Verdict(AMBER, "data_unavailable", False, dispute_open, signals)   # could not check the official list: never a false GREEN
    return Verdict(GREEN, "no_warning", False, dispute_open, signals)       # never "genuine", only "nothing found"

# ---- normalise + fuzzy manufacturer match ---------------------------------------
_REPL = [(r"\bm/s\b", ""), (r"\blimited\b", "ltd"), (r"\bprivate\b", "pvt")]
def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    for pat, rep in _REPL:
        s = re.sub(pat, rep, s)
    # keep letters, digits and combining marks (Devanagari/Kannada matras are marks, not word chars); everything else -> space
    s = "".join(c if (c.isalnum() or c.isspace() or unicodedata.category(c)[0] == "M") else " " for c in s)
    return re.sub(r"\s+", " ", s).strip()

def manufacturer_match(printed: str, canonical: str, threshold: float = 0.85):
    """Returns (matched: bool, score: 0..1). Compares against every known alias of the canonical name."""
    aliases = [canonical]
    try:
        row = db.one("SELECT alias_group_id FROM manufacturer_aliases WHERE alias_text = %s AND is_canonical LIMIT 1", (canonical,))
        if row:
            aliases = [r["alias_text"] for r in db.all_("SELECT alias_text FROM manufacturer_aliases WHERE alias_group_id = %s", (row["alias_group_id"],))]
    except Exception:
        snapshot.set_degraded(True)      # alias table unreachable: compare against the canonical name only
    p = normalise(printed)
    score = max(fuzz.token_sort_ratio(p, normalise(a)) for a in aliases) / 100
    return score >= threshold, round(score, 2)

# ---- DB-backed signal builders --------------------------------------------------------------
def _fmt(d): return d.strftime("%d/%m/%Y") if d else "unknown"

def _db(fn, default):
    """Run a live DB read; on any failure flag degraded mode and return `default` (the engine never raises on DB loss)."""
    try:
        return fn()
    except Exception:
        snapshot.set_degraded(True)
        return default

def _nsq_from_rows(batch: str, rows: list) -> Signal:
    active = next((r for r in rows if r["status"] == "active"), None)
    if active:
        conf = "structured" if active["extraction_confidence"] == "high" else active["extraction_confidence"]
        return Signal("nsq_status", "FAIL", f"Batch {batch} listed Not of Standard Quality ({active['reason']})",
                      "government_record", active["source_document"], conf,
                      last_updated=_fmt(active["alert_date"]), official=True, synthetic=active["data_origin"] == "synthetic")
    if rows:
        return Signal("nsq_status", "PASS", f"An earlier NSQ listing for this batch was {rows[0]['status']} — no current active listing",
                      "government_record", rows[0]["source_document"], "structured", last_updated=_fmt(rows[0]["alert_date"]),
                      synthetic=rows[0]["data_origin"] == "synthetic")
    return Signal("nsq_status", "PASS", "No match found in current CDSCO NSQ/spurious alerts",
                  "government_record", "Hamsa NSQ database", "structured")

def nsq_signal(batch: str) -> Optional[Signal]:
    """Latest CDSCO record for this batch. Rule 4: corrected/superseded rows never trigger RED, but history stays visible.
    Degraded mode: if the DB is unreachable, answer from the in-memory snapshot with a 'Data as of' prefix;
    with no snapshot either, return INCONCLUSIVE (resolve_conflicts turns that into AMBER, never GREEN)."""
    try:
        rows = db.all_("SELECT * FROM nsq_alerts WHERE batch_number = %s ORDER BY alert_date DESC", (batch,))
    except Exception:
        snapshot.set_degraded(True)
        as_of = snapshot.get_snapshot_date()
        if as_of is None:
            return Signal("nsq_status", "INCONCLUSIVE", "Live records unavailable and no saved copy — the official NSQ list could not be checked. Please confirm with a pharmacist.",
                          "government_record", "Hamsa NSQ database (unavailable)", "low", from_cache=True)
        sig = _nsq_from_rows(batch, snapshot.nsq_lookup(batch))
        sig.evidence_text = f"Data as of {as_of:%d %b %Y} — live records unavailable. {sig.evidence_text}"
        sig.source_reference = f"{sig.source_reference} (cached snapshot)"
        sig.from_cache = True
        if sig.status == "PASS":
            sig.confidence = "medium"
        return sig
    snapshot.set_degraded(False)
    if snapshot.get_snapshot_date() is None:
        snapshot.load_snapshot()
    return _nsq_from_rows(batch, rows)

def expiry_signal(expiry: Optional[date], source: str, confidence: str = "structured") -> Optional[Signal]:
    if not expiry:
        return None
    today = date.today()
    if expiry < today:
        months = max(1, (today.year - expiry.year) * 12 + today.month - expiry.month)
        return Signal("expiry_status", "EXPIRED", f"Expired on {_fmt(expiry)} ({months} month{'s' if months != 1 else ''} ago)",
                      "decoded_code", source, confidence)
    return Signal("expiry_status", "PASS", f"Expiry {_fmt(expiry)} — not yet expired",
                  "decoded_code", source, confidence)

_in_external_replay = False

def _builtin_replay_signal(batch: str) -> Signal:
    """Batch-level preview: the seed tag impossible_travel_demo. (replay.py adds serial-level checks.)"""
    hit = _db(lambda: db.one("SELECT 1 FROM scan_events WHERE batch_number = %s AND 'impossible_travel_demo' = ANY(risk_reasons) LIMIT 1", (batch,)), "unavailable")
    if hit == "unavailable":
        return Signal("replay_pattern", "INCONCLUSIVE", "[Preview] Scan-log check unavailable right now",
                      "scan_log", "Synthetic scan-log dataset (preview only)", "low", synthetic=True)
    if hit:
        return Signal("replay_pattern", "FLAGGED", "[Preview] Same serial seen in two locations >1,000 km apart within a day",
                      "scan_log", "Synthetic scan-log dataset (preview only)", "low", synthetic=True)
    return Signal("replay_pattern", "INCONCLUSIVE", "[Preview] No pattern flagged — synthetic scan-log demo data only",
                  "scan_log", "Synthetic scan-log dataset (preview only)", "low", synthetic=True)

def replay_signal(batch: str, serial: Optional[str] = None) -> Signal:
    """Delegates to replay.replay_signal when that module exists; otherwise the built-in batch-level preview.
    The import is done at call time so module import order / circular imports can never disable it."""
    global _in_external_replay
    if not _in_external_replay:
        try:
            from replay import replay_signal as ext
        except ImportError:
            ext = None
        if ext is not None:
            _in_external_replay = True            # guard: replay.py may itself fall back to engine.replay_signal
            try:
                return ext(batch, serial)
            except Exception:
                snapshot.set_degraded(True)       # DB failure inside the preview must not break the verdict
                return Signal("replay_pattern", "INCONCLUSIVE", "[Preview] Scan-log check unavailable right now",
                              "scan_log", "Synthetic scan-log dataset (preview only)", "low", synthetic=True)
            finally:
                _in_external_replay = False
    return _builtin_replay_signal(batch)

def open_dispute(batch: str) -> bool:
    return bool(_db(lambda: db.one("SELECT 1 FROM disputes WHERE batch_number = %s AND status IN ('open','under_review') LIMIT 1", (batch,)), None))

def batch_record(batch: str):
    return _db(lambda: db.one("""SELECT b.batch_number, b.expiry_date, b.manufacturer, p.brand_name, p.generic_name, p.product_code
                       FROM batches b JOIN products p USING (product_code) WHERE b.batch_number = %s""", (batch,)), None)

def product_by_gtin(gtin: str):
    """Product row for a GS1 GTIN (13- or 14-digit form). A GS1 code carries no manufacturer; the products table does."""
    g = (gtin or "").strip()
    return _db(lambda: db.one("SELECT * FROM products WHERE product_code IN (%s, %s) LIMIT 1", (g, g.lstrip("0"))), None)

def verify_manual(batch: str, expiry_typed: Optional[date] = None) -> tuple:
    """Manual-entry path: only two independent signals exist (official NSQ + expiry). Returns (Verdict, record)."""
    batch = batch.replace("\x00", "").strip().upper()      # NUL cannot be stored/compared in Postgres text; never let input look like a DB outage
    signals = [nsq_signal(batch)]
    rec = batch_record(batch)
    exp, src = (rec["expiry_date"], "Batch register") if rec else (expiry_typed, "Typed by you (unverified)")
    e = expiry_signal(exp, src, "structured" if rec else "low")
    if e:
        signals.append(e)
    if rec or _db(lambda: db.one("SELECT 1 FROM scan_events WHERE batch_number=%s LIMIT 1", (batch,)), None):
        signals.append(replay_signal(batch))     # preview row, always labelled
    v = resolve_conflicts(signals, open_dispute(batch))
    for s in v.signals:
        s.dispute = "open" if (v.dispute_open and s.signal_type == "nsq_status") else "none"
    return v, rec

def log_scan(v: Verdict, batch: str, product_code: str = "") -> str:
    """Persist scan + evidence (anonymous: scan_events has no user column). Returns scan_id for reports.
    If the DB is unreachable nothing is logged and "" is returned (a verdict must still be delivered)."""
    import hashlib
    try:
        sid = db.one("""INSERT INTO scan_events (id, hashed_serial_id, product_code, batch_number, approx_location, scanned_at, source, risk_level, risk_reasons)
                        VALUES (gen_random_uuid(), %s, %s, %s, 'n/a (no location opt-in)', now(), 'whatsapp', %s, %s) RETURNING id""",
                     (hashlib.sha256(batch.encode()).hexdigest()[:32], product_code or "unknown", batch, v.verdict.lower(), [v.reason]))["id"]
        for s in v.signals:
            db.run("""INSERT INTO scan_evidence (id, scan_id, signal_type, status, evidence_text, source_type, source_reference, confidence, created_at)
                      VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, now())""",
                   (sid, s.signal_type, _STATUS.get(s.status, s.status), s.evidence_text, s.source_type, s.source_reference, s.confidence))
    except Exception:
        snapshot.set_degraded(True)
        return ""
    return str(sid)

_STATUS = {"PASS": "Pass", "EXPIRED": "FAIL -- Expired", "INCONCLUSIVE": "Inconclusive", "FLAGGED": "Flagged (preview)"}  # match seed wording
