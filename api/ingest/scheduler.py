"""Daily CDSCO NSQ ingestion job, DB loader and health check.

  python -m ingest.scheduler --once     # run one ingestion now (from api/)
  ingest.scheduler.start()              # background thread, one run per 24 h (ENABLE_SCHEDULER=1)

Every run is one row in nsq_ingest_runs. Health check: WARNING if no run added new rows for more than 48 h.
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("hamsa.ingest")

INTERVAL_S = 24 * 3600            # daily only: never configurable below this
STALE_AFTER_H = 48
_stale = False


def _db():
    import db
    return db


# ---- DB loading -----------------------------------------------------------------------------
def load_rows(rows: list[dict], confidence: str = "medium", dry_run: bool = False) -> dict:
    """Insert accepted rows into nsq_alerts as data_origin='cdsco_real', status 'active'. Idempotent on
    (batch, manufacturer, alert_date): existing real rows are left alone except that a different confidence is updated."""
    db = _db()
    out = {"inserted": 0, "existing": 0, "confidence_updated": 0}
    for r in rows:
        key = (r["batch_number"].strip().upper(), r["manufacturer"].strip().lower(), str(r["alert_date"]))
        ex = db.one("""SELECT id, extraction_confidence FROM nsq_alerts WHERE data_origin='cdsco_real' AND upper(batch_number)=%s
                       AND lower(manufacturer)=%s AND alert_date=%s""", key)
        if ex:
            out["existing"] += 1
            if ex["extraction_confidence"] != confidence and not dry_run:
                db.run("UPDATE nsq_alerts SET extraction_confidence=%s WHERE id=%s", (confidence, ex["id"]))
                out["confidence_updated"] += 1
            continue
        out["inserted"] += 1
        if dry_run:
            continue
        db.run("""INSERT INTO nsq_alerts (id, product_name, batch_number, manufacturer, alert_date, reason, source_document,
                      extraction_confidence, lab_type, status, data_origin)
                  VALUES (gen_random_uuid(), %s,%s,%s,%s,%s,%s,%s,%s,'active','cdsco_real')""",
               (r["product_name"], key[0], r["manufacturer"], r["alert_date"], r["reason"], r["source_document"],
                confidence, r["lab_type"]))
    return out


def record_run(source: str, rows_new: int, status: str, note: str, started: datetime) -> None:
    _db().run("INSERT INTO nsq_ingest_runs (started_at, finished_at, source, rows_new, status, note) VALUES (%s, now(), %s, %s, %s, %s)",
              (started, source, rows_new, status, note[:1000]))


# ---- one run --------------------------------------------------------------------------------
def run_once(months: int = 3, confidence: str = "medium") -> dict:
    from ingest import cdsco, parse
    started = datetime.now(timezone.utc)
    try:
        paths = cdsco.fetch_latest(months=months)
        accepted, rejected, _ = parse.parse_dir(cdsco.PDF_DIR)
        res = load_rows(accepted, confidence)
        note = f"{len(paths)} PDFs; {len(accepted)} accepted, {len(rejected)} rejected; {res['existing']} already present"
        record_run(cdsco.PAGE_URL, res["inserted"], "ok" if res["inserted"] else "no_new_rows", note, started)
        if res["inserted"]:
            try:
                import snapshot
                snapshot.load_snapshot()
            except Exception:
                log.exception("snapshot reload failed after ingestion")
        log.info("ingest run: %s", note)
        return {"status": "ok", **res, "accepted": len(accepted), "rejected": len(rejected)}
    except Exception as e:                               # site down / blocked / layout changed: record it, keep serving
        log.exception("ingest run failed")
        try:
            record_run("cdsco", 0, "error", f"{type(e).__name__}: {e}", started)
        except Exception:
            log.exception("could not record failed run")
        return {"status": "error", "error": str(e)}


# ---- health ---------------------------------------------------------------------------------
def health_check(now: datetime | None = None) -> dict:
    """WARNING + stale flag if no ingest run added rows within STALE_AFTER_H hours. Returns the state."""
    global _stale
    now = now or datetime.now(timezone.utc)
    row = _db().one("SELECT max(finished_at) AS t FROM nsq_ingest_runs WHERE rows_new > 0")
    last = row["t"] if row else None
    stale = last is None or (now - last) > timedelta(hours=STALE_AFTER_H)
    if stale:
        log.warning("NSQ ingestion health: no new NSQ rows for more than %d h (last new rows: %s)", STALE_AFTER_H, last or "never")
    _stale = stale
    try:                                                 # let the snapshot/banner layer see it when it supports the flag
        import snapshot
        if hasattr(snapshot, "set_ingest_stale"):
            snapshot.set_ingest_stale(stale)
    except Exception:
        pass
    return {"stale": stale, "last_new_rows_at": last}


def is_stale() -> bool:
    return _stale


# ---- background loop ------------------------------------------------------------------------
def _loop(stop: threading.Event):
    while not stop.is_set():
        run_once()
        health_check()
        stop.wait(INTERVAL_S)


def start() -> threading.Event:
    """Start the daily background loop; returns the Event that stops it."""
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop,), daemon=True, name="nsq-ingest").start()
    return stop


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logging.basicConfig(level=logging.INFO)
    if "--once" in sys.argv:
        print(run_once())
        print(health_check())
    else:
        sys.exit("usage: python -m ingest.scheduler --once")
