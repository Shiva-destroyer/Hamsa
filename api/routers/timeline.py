"""GET /api/batch-timeline/{batch} — public, read-only, no report data."""
import re

from fastapi import APIRouter, HTTPException

import timeline

router = APIRouter()
_BATCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,39}$")


@router.get("/api/batch-timeline/{batch}")
def batch_timeline(batch: str):
    if not _BATCH.match(batch):
        raise HTTPException(status_code=400, detail="invalid batch number")
    t = timeline.timeline_json(batch)
    if not t["found"]:
        raise HTTPException(status_code=404, detail=f"no records for batch {t['batch']}")
    return t
