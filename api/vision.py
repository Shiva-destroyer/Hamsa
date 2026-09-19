"""Photo path: strip EXIF -> decode QR/DataMatrix -> OCR the printed label -> label-vs-code signal -> photo-quality heuristics.
Never guesses: unreadable fields are reported as INCONCLUSIVE ("please retake / type it"), not as a pass or a fail."""
import calendar
import io
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageOps

import engine
from engine import Signal

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 60_000_000
MAX_SIDE = 3000                 # larger photos are downscaled for decoding/OCR

# ---- heuristic thresholds (tuned on the generated fixtures in tests/test_vision.py; see comments there) ----
LOWRES_MIN_SHORT = 600          # px, short side of the photo as received
BLUR_VAR_MIN = 100.0            # variance of the Laplacian (tile-percentile, image scaled to 1600 px long side)
EDGE_MARGIN_FRAC = 0.01         # a code / label box within 1% of an image edge counts as touching it
OCR_MIN_CONF = 60               # mean Tesseract word confidence below this = "could not read", never guessed


class ImageError(ValueError):
    """The bytes are not a usable JPEG/PNG/WebP (or are too large)."""


# ---------------------------------------------------------------------------------------------
# image loading
# ---------------------------------------------------------------------------------------------
def _load(raw: bytes) -> Image.Image:
    """Decode, apply EXIF orientation, flatten to RGB and rebuild from pixels only (drops EXIF incl. GPS)."""
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ImageError("image missing or larger than 10 MB")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in ("JPEG", "PNG", "WEBP"):
                raise ImageError("unsupported image type")
            if im.width * im.height > MAX_PIXELS:
                raise ImageError("image dimensions too large")
            im.load()
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            else:
                im = im.convert("RGB")
            return Image.fromarray(np.asarray(im))
    except ImageError:
        raise
    except Exception as e:                       # truncated / corrupt / decompression bomb
        raise ImageError("invalid image") from e


def strip_exif(raw: bytes) -> bytes:
    """Re-encode pixels only: drops EXIF incl. GPS. Call on ingestion, before anything is stored."""
    out = io.BytesIO()
    _load(raw).save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------------------------
# code decoding
# ---------------------------------------------------------------------------------------------
@dataclass
class Code:
    text: str
    fmt: str                                # QRCode | DataMatrix
    box: tuple                              # x0, y0, x1, y1 in image pixels


def _box(points, scale=1.0):
    xs, ys = [p[0] / scale for p in points], [p[1] / scale for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def decode_codes(bgr: np.ndarray) -> list:
    """QR + DataMatrix via zxing-cpp (several preprocessing passes), then OpenCV QR as a fallback."""
    codes: list = []
    try:
        import zxingcpp
        fmts = zxingcpp.BarcodeFormat.QRCode | zxingcpp.BarcodeFormat.DataMatrix
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        passes = [(gray, 1.0), (cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 2.0),
                  (cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1], 1.0)]
        for arr, scale in passes:
            for r in zxingcpp.read_barcodes(arr, formats=fmts, text_mode=zxingcpp.TextMode.Plain):
                p = r.position
                codes.append(Code(r.text, "DataMatrix" if "Matrix" in str(r.format) else "QRCode",
                                  _box([(q.x, q.y) for q in (p.top_left, p.top_right, p.bottom_right, p.bottom_left)], scale)))
            if codes:
                break
    except ImportError:
        pass
    if not codes:                                # OpenCV QR fallback (also gives a box for undecodable codes)
        det = cv2.QRCodeDetector()
        for img in (bgr, cv2.resize(bgr, None, fx=2, fy=2)):
            try:
                text, pts, _ = det.detectAndDecode(img)
            except cv2.error:
                continue
            if text:
                s = img.shape[1] / bgr.shape[1]
                codes.append(Code(text, "QRCode", _box(pts.reshape(-1, 2), s) if pts is not None else (0, 0, 0, 0)))
                break
    return codes


def _undecoded_code_box(bgr: np.ndarray):
    """Box of a QR-like finder pattern that could not be decoded (used only for the crop check)."""
    try:
        ok, pts = cv2.QRCodeDetector().detect(bgr)
    except cv2.error:
        return None
    return _box(pts.reshape(-1, 2)) if ok and pts is not None else None


def decode_qr(png: bytes) -> Optional[str]:
    """Back-compat helper: text of the first code found in a PNG/JPEG byte string."""
    bgr = cv2.cvtColor(np.asarray(_load(png)), cv2.COLOR_RGB2BGR)
    codes = decode_codes(bgr)
    return codes[0].text if codes else None


# ---------------------------------------------------------------------------------------------
# GS1 parsing
# ---------------------------------------------------------------------------------------------
_FIXED = {"00": 18, "01": 14, "02": 14, "11": 6, "12": 6, "13": 6, "15": 6, "16": 6, "17": 6, "20": 2}
_VARIABLE = {"10": 20, "21": 20, "22": 20, "30": 8, "37": 8, "240": 30, "241": 30, "242": 6, "250": 30, "251": 30,
             "253": 30, "254": 20, "400": 30, "91": 90, "92": 90, "93": 90, "94": 90, "95": 90, "96": 90, "97": 90, "98": 90, "99": 90}
_GS = "\x1d"


def _gs1_fields(payload: str):
    """AI -> value dict from either syntax, or None if malformed."""
    p = (payload or "").strip()
    p = re.sub(r"^\](?:d2|Q3|C1|e0)", "", p)                       # symbology identifier, if the scanner left it
    p = p.replace("<GS>", _GS).replace("␝", _GS)
    if "(" in p:                                                    # parenthesised human-readable syntax
        if not re.fullmatch(r"(?:\(\d{2,4}\)[^()\x1d]+)+", p.replace(" ", "")):
            return None
        pairs = re.findall(r"\((\d{2,4})\)([^()\x1d]+)", p.replace(" ", ""))
        out: dict = {}
        for ai, val in pairs:
            if ai in out:
                return None
            out[ai] = val
        return out
    out, i = {}, 0                                                  # raw syntax: fixed-length AIs, GS-terminated variable ones
    while i < len(p):
        if p[i] == _GS:
            i += 1
            continue
        ai = next((a for a in (p[i:i + 4], p[i:i + 3], p[i:i + 2]) if a in _FIXED or a in _VARIABLE), None)
        if ai is None or ai in out:
            return None
        i += len(ai)
        if ai in _FIXED:
            val = p[i:i + _FIXED[ai]]
            if len(val) != _FIXED[ai] or not val.isdigit():
                return None
        else:
            end = p.find(_GS, i)
            end = len(p) if end < 0 else end
            val = p[i:end]
            if not val or len(val) > _VARIABLE[ai]:
                return None
        out[ai] = val
        i += len(val)
    return out


def parse_gs1(payload: str):
    """GTIN(01) + batch(10) + expiry(17 YYMMDD) [+ serial(21)] from raw GS1 (\\x1d / FNC1 separators) or parenthesised syntax.
    Returns None when malformed or a required field is missing. NOTE: no manufacturer name in the code;
    the manufacturer is resolved from the GTIN via the products table."""
    f = _gs1_fields(payload)
    if not f or not {"01", "10", "17"} <= f.keys() or not re.fullmatch(r"\d{14}|\d{13}", f["01"]) or not re.fullmatch(r"\d{6}", f["17"]):
        return None
    yy, mm, dd = int(f["17"][:2]), int(f["17"][2:4]), int(f["17"][4:6])
    if not 1 <= mm <= 12:
        return None
    dd = calendar.monthrange(2000 + yy, mm)[1] if dd == 0 else dd     # GS1: day 00 = last day of the month
    try:
        expiry = date(2000 + yy, mm, dd)
    except ValueError:
        return None
    batch = f["10"].strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9\-/._]{0,19}", batch):
        return None
    return {"gtin": f["01"].lstrip("0") if len(f["01"]) == 14 else f["01"], "batch": batch, "expiry": expiry, "serial": f.get("21", "")}


# ---------------------------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------------------------
@dataclass
class Word:
    text: str
    conf: float
    box: tuple
    line: tuple


def ocr_words(rgb: Image.Image, lang: str = "eng", config: str = "", timeout: int = 30) -> Optional[list]:
    """Tesseract words with confidence and boxes (original-image pixels). None = OCR engine unavailable/failed (not the same as 'no text')."""
    try:
        import pytesseract
        w, h = rgb.size
        s = min(1.0, 2000 / max(w, h))
        img = rgb.resize((int(w * s), int(h * s))) if s < 1 else rgb
        d = pytesseract.image_to_data(img, lang=lang, config=config, output_type=pytesseract.Output.DICT, timeout=timeout)
    except Exception:
        return None
    words = []
    for i, t in enumerate(d["text"]):
        t = (t or "").strip()
        if not t or float(d["conf"][i]) < 0:
            continue
        x, y, ww, hh = (d[k][i] / s for k in ("left", "top", "width", "height"))
        words.append(Word(t, float(d["conf"][i]), (x, y, x + ww, y + hh), (d["block_num"][i], d["par_num"][i], d["line_num"][i])))
    return words


def ocr_text(png: bytes) -> str:
    """Back-compat helper: plain OCR text of an image ('' if OCR is unavailable)."""
    words = ocr_words(_load(png))
    return "\n".join(l for l, _ in _lines(words or []))


def _lines(words: list) -> list:
    """[(line_text, [Word, ...])] in reading order."""
    groups: dict = {}
    for w in words:
        groups.setdefault(w.line, []).append(w)
    return [(" ".join(x.text for x in ws), ws) for _, ws in sorted(groups.items())]


def printed_manufacturer(text: str):
    m = re.search(r"(?im)^[^A-Za-z0-9\n]*m(?:fd|fg|anufactured)\.?\s*by\s*[:\-]?\s*(.+)$", text)
    return m.group(1).strip() if m else None


_MFR_RE = re.compile(r"(?i)^[^A-Za-z0-9]*m(?:fd|fg|anufactured)\.?\s*by\s*[:\-]?\s*(.+)$")
_BATCH_RE = re.compile(r"(?i)\b(?:batch|b\.?\s?no|lot)\b\.?\s*(?:no\.?|number|#)?\s*[:.\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,19})")
_EXP_RE = re.compile(r"(?i)\b(?:exp(?:iry)?\.?(?:\s*date)?|use\s*before)\b\s*[:.\-]?\s*(.+)$")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def _parse_month_year(s: str):
    """(year, month) from '03/2028', '3-28', '03.2028', '12/03/2028' (dd/mm/yyyy), 'MAR 2028'. None if not recognisable."""
    s = s.strip()
    m = re.search(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})", s)
    if m:
        mo, y = int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})\s*[/\-.]\s*(\d{2,4})\b", s)
        if m:
            mo, y = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"([A-Za-z]{3,9})[\s.\-/,]*(\d{2,4})\b", s)
            if not m or m.group(1)[:3].lower() not in _MONTHS:
                return None
            mo, y = _MONTHS[m.group(1)[:3].lower()], int(m.group(2))
    y = y + 2000 if y < 100 else y
    return (y, mo) if 1 <= mo <= 12 and 2000 <= y <= 2100 else None


def _conf(ws: list) -> float:
    return sum(w.conf for w in ws) / len(ws) if ws else 0.0


@dataclass
class Printed:
    manufacturer: Optional[str] = None
    batch: Optional[str] = None
    expiry: Optional[tuple] = None          # (year, month)
    has_batch_field: bool = False           # a "Batch ..." pattern exists at all (used for MISSING_FIELDS)
    has_expiry_field: bool = False


def read_label(words: list) -> Printed:
    """Extract printed manufacturer / batch / expiry. A field whose words average below OCR_MIN_CONF is left None (never guessed)."""
    out = Printed()
    for text, ws in _lines(words):
        m = _MFR_RE.match(text)
        if m and out.manufacturer is None:
            tail = ws[-len(m.group(1).split()):]
            if _conf(tail) >= OCR_MIN_CONF:
                out.manufacturer = m.group(1).strip()
        m = _BATCH_RE.search(text)
        if m:
            out.has_batch_field = True
            tail = [w for w in ws if m.group(1) in w.text] or ws[-1:]
            if out.batch is None and _conf(tail) >= OCR_MIN_CONF:
                out.batch = m.group(1)
        m = _EXP_RE.search(text)
        if m:
            my = _parse_month_year(m.group(1))
            if my:
                out.has_expiry_field = True
                if out.expiry is None and _conf(ws[-max(1, len(m.group(1).split())):]) >= OCR_MIN_CONF:
                    out.expiry = my
    return out


_CONFUSABLE = str.maketrans("OIL", "011")


def _canon_batch(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper()).translate(_CONFUSABLE)      # O/0 and I/L/1 are classic OCR swaps


# ---------------------------------------------------------------------------------------------
# label consistency (manufacturer via GTIN, batch, expiry)
# ---------------------------------------------------------------------------------------------
def label_signal(printed: Printed, fields: dict, registered_mfr: Optional[str], words: Optional[list]) -> tuple:
    """Returns (Signal, notes). FAIL on any readable mismatch; PASS only when the manufacturer was actually compared and nothing differs;
    otherwise INCONCLUSIVE + OCR_LOW_CONF."""
    if words is None:
        return _label_inconclusive("OCR engine unavailable — please type the manufacturer or retake the photo"), ["OCR_LOW_CONF"]
    problems, matches = [], []
    mfr_compared = False
    if printed.manufacturer and registered_mfr:
        ok, score = engine.manufacturer_match(printed.manufacturer, registered_mfr)
        mfr_compared = True
        (matches if ok else problems).append(
            f"printed manufacturer {'matches' if ok else 'differs from'} the registered manufacturer for this code (fuzzy match, {score:.2f})")
    printed_batch = printed.batch
    if not printed_batch:                                            # fall back: is the decoded batch printed anywhere?
        blob = _canon_batch("".join(w.text for w in words))
        if len(_canon_batch(fields["batch"])) >= 4 and _canon_batch(fields["batch"]) in blob:
            printed_batch = fields["batch"]
    if printed_batch:
        ok = _canon_batch(printed_batch) == _canon_batch(fields["batch"])
        (matches if ok else problems).append("printed batch matches the code" if ok else f"printed batch '{printed_batch}' differs from code batch '{fields['batch']}'")
    if printed.expiry:
        want = (fields["expiry"].year, fields["expiry"].month)
        ok = printed.expiry == want
        (matches if ok else problems).append("printed expiry matches the code" if ok else
                                             f"printed expiry {printed.expiry[1]:02d}/{printed.expiry[0]} differs from code expiry {want[1]:02d}/{want[0]}")
    if problems:
        return Signal("label_consistency", "FAIL", _cap("; ".join(problems)), "ocr_reading",
                      "OCR of label vs decoded code / product registry", "medium"), []
    if mfr_compared:
        return Signal("label_consistency", "PASS", _cap("; ".join(matches)), "ocr_reading",
                      "OCR of label vs decoded code / product registry", "medium"), []
    why = "the code's GTIN is not in the product registry" if not registered_mfr else "could not read the printed manufacturer reliably"
    return _label_inconclusive(f"{_cap(why)} — please type the manufacturer or retake the photo"), ["OCR_LOW_CONF"]


def _cap(t: str) -> str:
    return t[:1].upper() + t[1:]


def _label_inconclusive(text: str) -> Signal:
    return Signal("label_consistency", "INCONCLUSIVE", text, "ocr_reading", "OCR of label", "low")


# ---------------------------------------------------------------------------------------------
# visual heuristics: blur / low resolution / crop / missing print fields
# ---------------------------------------------------------------------------------------------
def sharpness(bgr: np.ndarray) -> float:
    """Variance of the Laplacian, robust to white space: image scaled to <=1600 px long side, split in an 8x8 grid,
    75th percentile of the per-tile variances (a page is mostly blank; only the tiles with content say anything about focus)."""
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    s = min(1.0, 1600 / max(g.shape))
    if s < 1:
        g = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    lap = cv2.Laplacian(g, cv2.CV_64F)
    h, w = lap.shape
    tiles = [lap[y * h // 8:(y + 1) * h // 8, x * w // 8:(x + 1) * w // 8].var() for y in range(8) for x in range(8)]
    return float(np.percentile(tiles, 75))


def _touches_edge(box, w: int, h: int) -> bool:
    mx, my = max(2.0, EDGE_MARGIN_FRAC * w), max(2.0, EDGE_MARGIN_FRAC * h)
    return box[0] <= mx or box[1] <= my or box[2] >= w - mx or box[3] >= h - my


def visual_signal(bgr: np.ndarray, raw_size: tuple, code_boxes: list, words: Optional[list], printed: Printed) -> tuple:
    """Returns (Signal | None, notes) where notes are drawn from BLUR / CROP / MISSING_FIELDS."""
    h, w = bgr.shape[:2]
    notes, why = [], []
    short = min(raw_size)
    if short < LOWRES_MIN_SHORT:
        notes.append("BLUR"); why.append(f"resolution too low ({short}px short side, need {LOWRES_MIN_SHORT}px)")
    sharp = sharpness(bgr)
    if sharp < BLUR_VAR_MIN:
        if "BLUR" not in notes:
            notes.append("BLUR")
        why.append(f"photo is blurry (sharpness {sharp:.0f} < {BLUR_VAR_MIN:.0f})")
    label_box = None
    if words:
        conf_words = [x for x in words if x.conf >= OCR_MIN_CONF and len(x.text) >= 2]
        if conf_words:
            label_box = (min(x.box[0] for x in conf_words), min(x.box[1] for x in conf_words),
                         max(x.box[2] for x in conf_words), max(x.box[3] for x in conf_words))
    if any(_touches_edge(b, w, h) for b in code_boxes) or (label_box and _touches_edge(label_box, w, h)):
        notes.append("CROP"); why.append("the code or label runs off the edge of the photo")
    if words is not None and not (printed.has_batch_field or printed.has_expiry_field):
        notes.append("MISSING_FIELDS"); why.append("no batch number or expiry date could be found on the label")
    if not notes:
        return None, []
    conf = "medium" if len(why) >= 2 else "low"
    return Signal("visual_heuristic", "FAIL", "Photo quality: " + "; ".join(why) + ". Retake in good light, whole pack in frame.",
                  "user_photo", "Image analysis of the photo", conf), notes


# ---------------------------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------------------------
def _code_ok() -> Signal:
    return Signal("code_validity", "PASS", "Code decodes correctly; GTIN, batch and expiry present", "decoded_code", "Decoded from photo", "structured")


def _code_fail(payload):
    return Signal("code_validity", "FAIL", "Code missing, unreadable or missing required fields (GTIN / batch / expiry)",
                  "decoded_code", "Decoded payload" if payload else "No code found in photo", "medium")


def verify_qr_payload(payload: str):
    """/api/verify input_type=qr: the client already scanned the code. Same evidence as the photo path minus OCR/visual checks
    (so no label_consistency row). Returns (Verdict, record, notes)."""
    fields = parse_gs1(payload)
    if not fields:
        return engine.resolve_conflicts([_code_fail(payload)]), None, ["QR_FAIL"]
    sig = Signal("code_validity", "PASS", "Code payload parses correctly; GTIN, batch and expiry present", "decoded_code", "Scanned code payload", "structured")
    return _finish(fields, [sig], None, None)


def _finish(fields: dict, signals: list, extra: Optional[Signal], notes_in: Optional[list]):
    """Shared tail: NSQ -> expiry -> replay preview -> (visual) -> resolve. `signals` already holds code_validity (+ label_consistency)."""
    batch = fields["batch"]
    prod = engine.product_by_gtin(fields["gtin"])
    rec = engine.batch_record(batch)
    signals = list(signals) + [engine.nsq_signal(batch), engine.expiry_signal(fields["expiry"], "Decoded from code"),
                               engine.replay_signal(batch, fields["serial"] or None)]
    if extra:
        signals.append(extra)
    v = engine.resolve_conflicts(signals, engine.open_dispute(batch))
    return v, (rec or {"batch_number": batch, "brand_name": (prod or {}).get("brand_name", "Unknown product"), "product_code": fields["gtin"]}), list(notes_in or [])


def verify_photo(raw: bytes):
    """Returns (Verdict, record, notes[list of str]). Raises ImageError (a ValueError) for non-images / oversize input.
    notes may contain QR_FAIL, OCR_LOW_CONF, BLUR, CROP, MISSING_FIELDS."""
    rgb = _load(raw)
    raw_size = rgb.size
    if max(rgb.size) > MAX_SIDE:
        s = MAX_SIDE / max(rgb.size)
        rgb = rgb.resize((int(rgb.width * s), int(rgb.height * s)), Image.LANCZOS)
    bgr = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)

    codes = decode_codes(bgr)
    fields, payload = None, None
    for c in codes:
        payload = payload or c.text
        fields = parse_gs1(c.text)
        if fields:
            break
    code_boxes = [c.box for c in codes] or ([b] if (b := _undecoded_code_box(bgr)) else [])

    words = ocr_words(rgb)
    printed = read_label(words or [])
    vis, vnotes = visual_signal(bgr, raw_size, code_boxes, words, printed)

    if not fields:
        signals = [_code_fail(payload)] + ([vis] if vis else [])
        return engine.resolve_conflicts(signals), None, ["QR_FAIL"] + vnotes        # caller routes user to manual entry
    prod = engine.product_by_gtin(fields["gtin"])
    registered_mfr = (prod or engine.batch_record(fields["batch"]) or {}).get("manufacturer")   # GS1 has no manufacturer: resolve via GTIN
    lab, lnotes = label_signal(printed, fields, registered_mfr, words)
    return _finish(fields, [_code_ok(), lab], vis, lnotes + vnotes)
