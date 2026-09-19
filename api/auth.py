"""Regulator auth: prototype-grade shared secret -> 15-minute HS256 JWT. Say so in docs.

Env (read at call time): JWT_SECRET, REGULATOR_USERNAME, REGULATOR_SHARED_SECRET. If any is unset/empty, login and
token verification both fail closed."""
import hmac
import os
import time
from typing import Optional

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

TOKEN_TTL_S = 15 * 60
ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)


def _jwt_secret() -> str:
    return os.environ.get("JWT_SECRET", "")


def configured() -> bool:
    return all(os.environ.get(k) for k in ("JWT_SECRET", "REGULATOR_USERNAME", "REGULATOR_SHARED_SECRET"))


def check_credentials(username: str, secret: str) -> bool:
    """Constant-time on both fields (no short-circuit). False if the regulator account is not configured."""
    if not configured():
        return False
    u = hmac.compare_digest(username.encode(), os.environ["REGULATOR_USERNAME"].encode())
    s = hmac.compare_digest(secret.encode(), os.environ["REGULATOR_SHARED_SECRET"].encode())
    return u and s


def create_token(username: str) -> str:
    key = _jwt_secret()
    if not key:
        raise RuntimeError("JWT_SECRET not set")
    now = int(time.time())
    return jwt.encode({"sub": username, "role": "regulator", "iat": now, "exp": now + TOKEN_TTL_S}, key, algorithm=ALGORITHM)


def authenticate(creds: Optional[HTTPAuthorizationCredentials]) -> str:
    """Return the regulator username or raise 401 (missing/invalid/expired/unconfigured) / 403 (wrong role)."""
    hdr = {"WWW-Authenticate": "Bearer"}
    key = _jwt_secret()
    if not key or not creds or not creds.credentials:
        raise HTTPException(401, "Not authenticated", headers=hdr)
    try:
        claims = jwt.decode(creds.credentials, key, algorithms=[ALGORITHM], options={"require": ["exp", "iat", "sub"]})
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token", headers=hdr)
    if claims.get("role") != "regulator":
        raise HTTPException(403, "Forbidden")
    return claims["sub"]


def require_regulator(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> str:
    """FastAPI dependency for regulator-only routes."""
    return authenticate(creds)
