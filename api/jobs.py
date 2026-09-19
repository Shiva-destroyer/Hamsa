"""Retention jobs. Run in-process via APScheduler when ENABLE_SCHEDULER=1: call `start()` at app startup and
`stop()` at shutdown. Every job is also a plain function (returns rows affected) so it can be called directly.
Seed data (scan_events.origin='seed') is never touched. Attached uploads are not purged here."""
import logging

import db
import storage

log = logging.getLogger("hamsa.jobs")


def purge_unattached_uploads(days: int = 30) -> int:
    n = 0
    for u in db.all_("SELECT id, path FROM uploads WHERE attached_to_id IS NULL AND created_at < now() - make_interval(days => %s)", (days,)):
        if storage.delete_file(u["path"]):
            db.run("DELETE FROM uploads WHERE id = %s", (u["id"],))
            n += 1
    return n


def _count(sql: str, params=()) -> int:
    return db.one(f"WITH d AS ({sql} RETURNING 1) SELECT count(*) AS n FROM d", params)["n"]


def purge_dedupe(days: int = 7) -> int:
    return _count("DELETE FROM wa_inbound_dedupe WHERE received_at < now() - make_interval(days => %s)", (days,))


def purge_idle_sessions(days: int = 30) -> int:
    return _count("DELETE FROM wa_sessions WHERE last_seen_at < now() - make_interval(days => %s)", (days,))


def scrub_locations(days: int = 180) -> int:
    return _count("""UPDATE scan_events SET approx_location = 'scrubbed'
                     WHERE origin = 'live' AND scanned_at < now() - make_interval(days => %s) AND approx_location <> 'scrubbed'""", (days,))


def run_all() -> dict:
    out = {}
    for fn in (purge_unattached_uploads, purge_dedupe, purge_idle_sessions, scrub_locations):
        try:
            out[fn.__name__] = fn()
        except Exception:
            log.exception("retention job failed: %s", fn.__name__)
            out[fn.__name__] = None
    log.info("retention run: %s", out)
    return out


_scheduler = None


def start():
    """Start the background scheduler if ENABLE_SCHEDULER=1; returns it (or None)."""
    import os
    global _scheduler
    if os.environ.get("ENABLE_SCHEDULER", "0") != "1" or _scheduler:
        return _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(run_all, "interval", hours=6, id="retention", max_instances=1, coalesce=True)
    _scheduler.start()
    return _scheduler


def stop() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
