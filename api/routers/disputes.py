"""Public dispute banner data. Exposes only whether a batch has a dispute under review -- never the
submitter, evidence, notes or regulator decision text."""
from fastapi import APIRouter, Depends, HTTPException

import db
from ratelimit import ip_limit
from texts import t

router = APIRouter(prefix="/api/disputes", tags=["disputes"])


@router.get("/{batch}")
def dispute_banner(batch: str, lang: str = "en", _rl=Depends(ip_limit)):
    b = db.one("SELECT batch_number, dispute_status FROM batches WHERE batch_number = %s", (batch.strip().upper()[:40],))
    if not b:
        raise HTTPException(404, "Unknown batch")
    is_open = bool(db.one("SELECT 1 FROM disputes WHERE batch_number = %s AND status IN ('open','under_review') LIMIT 1", (b["batch_number"],)))
    return {"batch_number": b["batch_number"], "dispute_open": is_open, "dispute_status": b["dispute_status"],
            "banner": t("dispute_banner", lang) if is_open else None}
