"""In-memory snapshot of all NSQ rows.
Loaded at startup and after ingestion. When the DB is unreachable the engine answers from this snapshot instead of failing.
The snapshot is never used while the DB is healthy; it only carries the NSQ table (no batch register, no disputes)."""
import threading
from datetime import date
import db

_lock = threading.Lock()
_rows: list = []
_date: "date | None" = None
_degraded = False


def load_snapshot() -> None:
    """Reload every nsq_alerts row into memory. If the DB is down the previous snapshot (if any) is kept and degraded mode is set."""
    global _rows, _date, _degraded
    try:
        rows = db.all_("SELECT * FROM nsq_alerts ORDER BY alert_date DESC")
    except Exception:
        set_degraded(True)
        return
    with _lock:
        _rows = [dict(r) for r in rows]
        _date = date.today()
        _degraded = False


def get_snapshot_date() -> "date | None":
    return _date


def nsq_lookup(batch: str) -> list:
    """NSQ rows for the batch from memory, newest alert first (same order/shape as the live query)."""
    with _lock:
        return [dict(r) for r in _rows if r.get("batch_number") == batch]


def is_degraded() -> bool:
    """True when the most recent live DB call failed (cleared by the next successful one)."""
    return _degraded


def set_degraded(flag: bool) -> None:
    global _degraded
    _degraded = flag
