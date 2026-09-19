"""DELETE /api/user/data. The caller identifies itself with the irreversible identity hash
(header X-Identity-Hash, 64 hex chars) -- never a raw phone number. Reports/disputes already filed are not deleted."""
import re

from fastapi import APIRouter, Depends, Header, HTTPException

import privacy
from ratelimit import ip_limit

router = APIRouter(prefix="/api/user", tags=["user"])
_HASH = re.compile(r"^[0-9a-f]{64}$")


@router.delete("/data")
def delete_my_data(x_identity_hash: str = Header(None), _rl=Depends(ip_limit)):
    if not x_identity_hash or not _HASH.match(x_identity_hash):
        raise HTTPException(400, "X-Identity-Hash header (64 hex chars) required")
    return privacy.erase(x_identity_hash)
