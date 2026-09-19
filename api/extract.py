"""Full-text extraction from an uploaded photo/scan, all local (no external service). Florence-2 reads first (florence.py); when it is off,
unavailable, not confident or finds nothing, Tesseract does (OpenCV clean-up: deskew, contrast) -- the only reader for Hindi/Kannada.
Never guesses: Tesseract words below MIN_WORD_CONF are dropped, weak lines are marked with UNCLEAR, and an engine failure is ok=False (not 'no text')."""
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image

import florence
import vision

MIN_WORD_CONF = 30              # words below this are noise, dropped
SOLID_WORD_CONF = 60            # confident-word mass picks the best of the OCR runs; a line averaging below this is marked
UNCLEAR = "⚠ "
MIN_SHORT = 1000                # px: smaller photos are upscaled before OCR
MAX_SKEW = 15.0                 # degrees searched by deskew; beyond that the user should retake the photo
RUN_TIMEOUT = 20                # s per Tesseract run
RUNS = (("orig", "--psm 3"), ("clean", "--psm 3"), ("clean", "--psm 11"))     # (image variant, Tesseract page mode); 11 = sparse text on packaging


@dataclass
class Extracted:
    text: str
    lines: list                 # [(line_text, mean_conf)]
    mean_conf: float
    langs: str
    ok: bool                    # False = OCR engine unavailable/failed; True with text == "" means no text found
    engine: str = "tesseract"   # or "florence" (then mean_conf is its mean token probability x 100 and lines carry no per-line marks)


@lru_cache(maxsize=1)
def _langs() -> str:
    """eng+hin+kan for whichever of those Tesseract has installed."""
    try:
        import pytesseract
        have = set(pytesseract.get_languages(config=""))
    except Exception:
        return "eng"
    return "+".join(l for l in ("eng", "hin", "kan") if l in have) or "eng"


def _skew(gray: np.ndarray) -> float:
    """Angle (deg) that straightens the text lines: the rotation that makes the row-ink profile sharpest."""
    s = 600 / max(gray.shape)
    small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else gray
    ink = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    h, w = ink.shape
    best, best_score = 0.0, -1.0
    for a in np.arange(-MAX_SKEW, MAX_SKEW + 0.01, 0.5):
        rot = cv2.warpAffine(ink, cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0), (w, h))
        score = float(np.var(rot.sum(axis=1, dtype=np.float64)))
        if score > best_score:
            best, best_score = float(a), score
    return best


def _clean(rgb: Image.Image) -> Image.Image:
    """Grayscale -> upscale small photos -> deskew -> local contrast -> adaptive threshold (handles uneven phone lighting)."""
    gray = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2GRAY)
    s = MIN_SHORT / min(gray.shape)
    if s > 1:
        gray = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    a = _skew(gray)
    if abs(a) >= 0.5:
        h, w = gray.shape
        gray = cv2.warpAffine(gray, cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0), (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    return Image.fromarray(cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15))


def _blank_codes(rgb: Image.Image, codes: list) -> Image.Image:
    """Whiten decoded QR/DataMatrix areas: their modules only produce junk 'text'; the payload is reported instead."""
    if not codes:
        return rgb
    arr = np.array(rgb)
    for c in codes:
        x0, y0, x1, y1 = c.box
        m = 0.1 * max(x1 - x0, y1 - y0)
        arr[max(0, int(y0 - m)):int(y1 + m), max(0, int(x0 - m)):int(x1 + m)] = 255
    return Image.fromarray(arr)


def _mass(words: list) -> float:
    return sum(w.conf for w in words if w.conf >= SOLID_WORD_CONF)


def extract_text(raw: bytes) -> Extracted:
    """Everything legible in the image, in reading order. Raises vision.ImageError for a bad/oversize image."""
    rgb = vision._load(raw)
    if max(rgb.size) > vision.MAX_SIDE:
        f = vision.MAX_SIDE / max(rgb.size)
        rgb = rgb.resize((int(rgb.width * f), int(rgb.height * f)), Image.LANCZOS)
    codes = vision.decode_codes(cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR))
    rgb = _blank_codes(rgb, codes)
    code_lines = [f"[{c.fmt}] {c.text}" for c in codes]
    fl = florence.read_lines(rgb)
    if fl and fl[0]:
        lines, conf = fl
        return Extracted("\n".join(lines + code_lines), [(l, conf) for l in lines], conf, "eng", True, "florence")
    langs = _langs()
    images = {"orig": rgb}
    best = None
    for variant, psm in RUNS:
        if variant not in images:
            images[variant] = _clean(rgb)
        words = vision.ocr_words(images[variant], lang=langs, config=psm, timeout=RUN_TIMEOUT)
        if words is not None and (best is None or _mass(words) > _mass(best)):
            best = words
    if best is None and not codes:
        return Extracted("", [], 0.0, langs, False)
    lines = []
    for _, ws in vision._lines(best or []):
        ws = [w for w in ws if w.conf >= MIN_WORD_CONF]
        if ws:
            lines.append((" ".join(w.text for w in ws), sum(w.conf for w in ws) / len(ws)))
    text = "\n".join([(UNCLEAR if c < SOLID_WORD_CONF else "") + l for l, c in lines] + code_lines)
    return Extracted(text, lines, round(sum(c for _, c in lines) / len(lines), 1) if lines else 0.0, langs, True)
