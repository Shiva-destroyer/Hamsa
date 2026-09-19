"""Filter: keep only the fields the bot needs from a photo's text -- manufacturer, batch, expiry, licence no. -- and drop the rest.
Code payloads win over printed text. A field on a low-confidence line (extract.UNCLEAR) is dropped, never guessed.
match_register() finds a batch in the register through the indexed batch_key column and tolerates OCR letter/digit swaps."""
import itertools
import re
from dataclasses import dataclass
from typing import Optional

import db
import extract
import vision

MAX_LEN = 80                     # a reflected field is never longer than this
MAX_VARIANTS = 64                # cap on OCR-swap spellings tried against the index

_CODE = re.compile(r"^\[[^\]]+\]\s*(.+)$")
_BATCH = re.compile(r"(?i)\b(?:batch|b\.?\s?no|lot)\b[\s.:#\-]*(?:no\b|number\b)?[\s.:#\-]*([A-Za-z0-9][A-Za-z0-9\-/]{2,19})")
_LICENCE = re.compile(r"(?i)\blic(?:ence|ense)?\b\.?\s*(?:no\b\.?|number\b)?[\s.:\-]*([A-Za-z0-9][A-Za-z0-9/\-]{3,29})")
_MARK = re.compile(r"(?i)\bm(?:fd|fg|df|anufactured)\b\.?(?:\s+in\s+[A-Za-z]+)?\s*by\b\s*[:\-.]*\s*(.*)$")
_COMPANY = re.compile(r"(?i)\b(?:[lI1]td|limited|pvt|private|laborator\w+|labs?|lifesciences?|healthcare|pharmaceuticals?|remedies|industries)\b")
_LEGAL = re.compile(r"(?i)\b(?:[lI1]td|limited|pvt|private)\b")


@dataclass
class Fields:
    manufacturer: Optional[str] = None
    batch: Optional[str] = None
    expiry: Optional[tuple] = None          # (year, month)
    licence: Optional[str] = None

    def any(self) -> bool:
        return bool(self.manufacturer or self.batch or self.expiry or self.licence)


def _token(m: Optional[re.Match]) -> Optional[str]:
    return m.group(1) if m and any(ch.isdigit() for ch in m.group(1)) else None       # a batch/licence number always has a digit


def _manufacturer(lines: list) -> Optional[str]:
    for i, s in enumerate(lines):
        m = _MARK.search(s)
        if not m:
            continue
        tail = m.group(1).strip()
        for cand in ([tail] if len(re.findall(r"[A-Za-z]", tail)) >= 3 else []) + lines[i + 1:i + 4]:
            if _COMPANY.search(cand) and not _LICENCE.search(cand):
                return cand.strip(" :;,-")[:MAX_LEN]
    return next((s.strip(" :;,-")[:MAX_LEN] for s in lines if _LEGAL.search(s) and not _LICENCE.search(s)), None)


def filter_text(text: str) -> Fields:
    f, lines, codes = Fields(), [], []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith(extract.UNCLEAR.strip()):
            continue
        m = _CODE.match(s)
        (codes if m else lines).append(m.group(1) if m else s)
    for payload in codes:
        g = vision.parse_gs1(payload)
        if g:
            f.batch, f.expiry = g["batch"], (g["expiry"].year, g["expiry"].month)
            break
    for s in lines:
        f.batch = f.batch or _token(_BATCH.search(s))
        f.licence = f.licence or _token(_LICENCE.search(s))
        m = vision._EXP_RE.search(s)
        f.expiry = f.expiry or (vision._parse_month_year(m.group(1)) if m else None)
    f.manufacturer = _manufacturer(lines)
    return f


_SWAP = {"O": "0O", "0": "0O", "I": "1IL", "L": "1IL", "1": "1IL"}       # letter/digit slips OCR makes


def _keys(batch: str) -> list:
    key = re.sub(r"[^A-Z0-9]", "", batch.upper())
    opts = [_SWAP.get(c, c) for c in key]
    n = 1
    for o in opts:
        n *= len(o)
    return [] if not key else [key] if n > MAX_VARIANTS else ["".join(p) for p in itertools.product(*opts)]


def match_register(batch: str) -> Optional[dict]:
    """The one register batch this text can be (indexed lookup on batches.batch_key), else None -- two candidates is never a guess."""
    keys = _keys(batch)
    if not keys:
        return None
    rows = db.all_("SELECT b.batch_number, p.brand_name FROM batches b JOIN products p USING (product_code) WHERE b.batch_key = ANY(%s)", (keys,))
    return rows[0] if len(rows) == 1 else None
