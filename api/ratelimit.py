"""In-memory sliding-window rate limiter.

    check(key, limit, per_seconds) -> (allowed, retry_after)

Single-process only (the API runs as one uvicorn worker behind the Cloudflare Tunnel); state is lost on restart.
Only allowed calls are recorded, so a blocked client does not extend its own lockout.

FastAPI dependency for other routers:  `Depends(ip_limit)` -> 30/min/IP and 200/day/IP, 429 + Retry-After.

TRUST ASSUMPTION: the `CF-Connecting-IP` header is believed only because the API listens on localhost and is reached
exclusively through the Cloudflare Tunnel, which overwrites that header. If the API were ever exposed directly, a
client could forge the header and dodge the limit -- do not bind uvicorn to a public interface.
"""
import math
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

IP_PER_MINUTE = 30
IP_PER_DAY = 200

_lock = threading.Lock()
_hits: dict = {}                     # (key, per_seconds) -> deque[timestamps]
_MAX_KEYS = 10_000


def _now() -> float:                 # indirection so tests can move the clock
    return time.monotonic()


def check(key: str, limit: int, per_seconds: int):
    """Record one hit for `key` if fewer than `limit` hits happened in the last `per_seconds`.
    Returns (True, 0) when allowed, else (False, seconds_until_a_slot_frees)."""
    now = _now()
    with _lock:
        if len(_hits) > _MAX_KEYS:
            for k in [k for k, d in _hits.items() if not d or d[-1] <= now - k[1]]:
                del _hits[k]
        dq = _hits.setdefault((key, per_seconds), deque())
        while dq and dq[0] <= now - per_seconds:
            dq.popleft()
        if len(dq) < limit:
            dq.append(now)
            return True, 0
        return False, max(1, math.ceil(dq[0] + per_seconds - now))


def reset() -> None:
    with _lock:
        _hits.clear()


def client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


def ip_limit(request: Request) -> None:
    """FastAPI dependency: 30/min and 200/day per client IP. Minute is checked first so calls refused by the
    minute cap are never counted against the daily cap."""
    ip = client_ip(request)
    for suffix, limit, per in (("min", IP_PER_MINUTE, 60), ("day", IP_PER_DAY, 86400)):
        ok, retry = check(f"ip:{ip}:{suffix}", limit, per)
        if not ok:
            raise HTTPException(429, "Too many requests", headers={"Retry-After": str(retry)})
