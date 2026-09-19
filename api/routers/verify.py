"""POST /api/verify: the same Evidence Engine the WhatsApp flow uses, exposed as JSON.
Rate limits are attached by the app (Depends(ip_limit)); this router does not implement any."""
import base64
import binascii
import dataclasses
import os
import re
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import engine
import snapshot
import texts
import vision

router = APIRouter()

VOICE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "voice")
MAX_PHOTO_BYTES = vision.MAX_IMAGE_BYTES
BATCH_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-/]{3,19}$", re.I)          # same rule as the WhatsApp manual path


class VerifyRequest(BaseModel):
    input_type: Literal["manual", "qr", "photo"]
    batch_number: Optional[str] = Field(None, max_length=64)
    product_code: Optional[str] = Field(None, max_length=32)       # accepted for client convenience; the code's GTIN / register decide
    qr_payload: Optional[str] = Field(None, max_length=2000)
    photo_base64: Optional[str] = None
    user_language: Literal["en", "hi", "kn"] = "en"


def _voice_url(vk: str, lang: str) -> Optional[str]:
    """Static /voice/{file}.ogg URL, only when that pre-generated file exists (voice notes are produced by scripts/gen_voice.py)."""
    for l in (lang, "en"):
        name = f"{vk}_{l}.ogg"
        if os.path.isfile(os.path.join(VOICE_DIR, name)):
            return f"/voice/{name}"
    return None


def _decode_photo(b64: str) -> bytes:
    b64 = re.sub(r"^data:[^,]*,", "", b64.strip())
    if len(b64) > MAX_PHOTO_BYTES * 4 // 3 + 8:
        raise HTTPException(413, "photo larger than 10 MB")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "photo_base64 is not valid base64")
    if len(raw) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "photo larger than 10 MB")
    return raw


@router.post("/api/verify")
def verify(req: VerifyRequest):
    notes: list = []
    if req.input_type == "manual":
        if not req.batch_number or not BATCH_RE.match(req.batch_number.strip()):
            raise HTTPException(422, "batch_number must be 4-20 characters: letters, digits, '-' or '/'")
        v, rec = engine.verify_manual(req.batch_number)
    elif req.input_type == "qr":
        if not req.qr_payload:
            raise HTTPException(422, "qr_payload is required for input_type=qr")
        v, rec, notes = vision.verify_qr_payload(req.qr_payload)
    else:
        if not req.photo_base64:
            raise HTTPException(422, "photo_base64 is required for input_type=photo")
        try:
            v, rec, notes = vision.verify_photo(_decode_photo(req.photo_base64))
        except vision.ImageError as e:
            raise HTTPException(422, f"invalid image: {e}")

    lang = req.user_language
    vk = texts.verdict_key(v)
    action = texts.VERDICT[vk]["action"]
    action_text = action.get(lang) or action["en"]
    cached = any(s.from_cache for s in v.signals)
    as_of = snapshot.get_snapshot_date() if cached else None
    if cached and as_of:
        action_text = f"Data as of {as_of:%d %b %Y} — live records unavailable. {action_text}"
    demo = bool(rec and "expiry_date" in rec) or any(s.synthetic and s.signal_type == "nsq_status" for s in v.signals)
    return {
        "verdict": v.verdict,
        "verdict_reason": v.reason,
        "also_expired": v.also_expired,
        "dispute_open": v.dispute_open,
        "escalated_from": v.escalated_from,
        "evidence_matrix": [dataclasses.asdict(s) for s in v.signals],
        "safe_action": {"text": action_text, "voice_note_url": _voice_url(vk, lang), "report_enabled": not cached},
        "served_from_cache": cached,
        "cache_last_updated": as_of.isoformat() if as_of else None,
        "demo_notice": texts.t("demo_tag", "en").strip("_") if demo else None,
        "notes": notes,
    }
