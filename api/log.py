"""Structured logging: one JSON line per event on stdout.

    event(name, **fields) -> None

Never logs raw phone numbers, tokens or secrets:
  * fields whose NAME looks sensitive (phone, token, secret, to, sender ...) are replaced by "[redacted]";
  * string values are scrubbed: phone-like digit runs, "Bearer ..." headers and the configured secret values;
  * callers should identify people by a short hash prefix only (see flow.who()).
Logging can never raise into the caller."""
import json
import re
import sys
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
REDACTED = "[redacted]"
_KEY_PARTS = ("token", "secret", "authorization", "password", "api_key", "apikey", "phone", "msisdn", "bearer", "wa_id", "email")
_KEY_EXACT = {"to", "from", "sender", "recipient", "number", "raw"}
_PHONE = re.compile(r"(?<![\w])\+?\d(?:[ \-]?\d){9,14}(?!\w)")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]+")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_MAXLEN = 500


def _secret_values() -> list:
    try:
        import config
        vals = [config.WA_ACCESS_TOKEN, config.WA_APP_SECRET, config.IDENTITY_SECRET, config.WA_VERIFY_TOKEN]
    except Exception:
        return []
    return [v for v in vals if v and len(v) >= 6]


def _scrub_str(s: str, secrets: list) -> str:
    for v in secrets:
        s = s.replace(v, REDACTED)
    s = _BEARER.sub("Bearer " + REDACTED, s)
    s = _EMAIL.sub(REDACTED, s)
    s = _PHONE.sub(REDACTED, s)
    return s[:_MAXLEN]


def _sensitive_key(k: str) -> bool:
    k = str(k).lower()
    return k in _KEY_EXACT or any(p in k for p in _KEY_PARTS)


def _clean(v, secrets, depth=0):
    if depth > 4:
        return REDACTED
    if isinstance(v, str):
        return _scrub_str(v, secrets)
    if isinstance(v, bool) or v is None or isinstance(v, float):
        return v
    if isinstance(v, int):
        return REDACTED if len(str(abs(v))) >= 10 else v
    if isinstance(v, dict):
        return {str(k): (REDACTED if _sensitive_key(k) else _clean(x, secrets, depth + 1)) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_clean(x, secrets, depth + 1) for x in list(v)[:50]]
    return _scrub_str(str(v), secrets)


def event(name: str, **fields) -> None:
    try:
        secrets = _secret_values()
        rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "event": name}
        for k, v in fields.items():
            rec[k] = REDACTED if _sensitive_key(k) else _clean(v, secrets)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except Exception:
        pass
