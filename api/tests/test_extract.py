"""Full-text extraction (extract.py): deskew/clean-up + local OCR, code payloads reported, never-guess conventions.
Fixtures reuse the generated pack look-alike from test_vision (no database needed)."""
import pytest
from PIL import Image

import extract
import vision
from test_vision import blur, render_pack, to_bytes

CODE = "(01)08907573869302(10)AX2291(17)280331(21)SN0001"


def read(im):
    return extract.extract_text(to_bytes(im))


def test_clean_pack_all_text_and_code_payload():
    x = read(render_pack(CODE))
    assert x.ok and x.mean_conf > 80
    for token in ("Testomol", "Paracetamol", "Cipla", "AX2291", "03/2028", "NOT A REAL MEDICINE"):
        assert token in x.text
    assert f"[QRCode] {CODE}" in x.text
    assert not any(l.startswith(extract.UNCLEAR) for l in x.text.splitlines()), "QR modules must not leak in as junk text"


@pytest.mark.parametrize("angle", [7, -10])
def test_rotated_photo_is_straightened(angle):
    im = render_pack(CODE).rotate(angle, expand=True, fillcolor="white")
    x = read(im)
    assert "Batch No.: AX2291" in x.text and "03/2028" in x.text


def test_mildly_blurred_photo_still_reads_batch_and_expiry():
    x = read(blur(render_pack(CODE), 1.5))
    assert "AX2291" in x.text and "03/2028" in x.text


def test_small_photo_is_upscaled():
    x = read(render_pack(CODE).resize((480, 700)))
    assert "AX2291" in x.text


def test_blank_image_is_ok_with_no_text():
    x = read(Image.new("RGB", (800, 600), "white"))
    assert x.ok and x.text == "" and x.lines == []


def test_engine_failure_is_not_reported_as_no_text(monkeypatch):
    monkeypatch.setattr(vision, "ocr_words", lambda *a, **k: None)
    x = read(Image.new("RGB", (800, 600), "white"))
    assert not x.ok and x.text == ""


def test_low_confidence_line_is_marked_not_presented_as_fact(monkeypatch):
    W = vision.Word
    words = [W("Batch", 95, (0, 0, 1, 1), (1, 1, 1)), W("smudge", 40, (0, 0, 1, 1), (1, 1, 2)), W("noise", 10, (0, 0, 1, 1), (1, 1, 3))]
    monkeypatch.setattr(vision, "ocr_words", lambda *a, **k: words)
    x = read(Image.new("RGB", (800, 600), "white"))
    assert x.text.splitlines() == ["Batch", extract.UNCLEAR + "smudge"]       # 'noise' (conf < 30) dropped


def test_installed_languages_are_detected(monkeypatch):
    import pytesseract
    extract._langs.cache_clear()
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["eng", "hin", "kan", "osd"])
    assert extract._langs() == "eng+hin+kan"
    extract._langs.cache_clear()
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["eng", "osd"])
    assert extract._langs() == "eng"
    extract._langs.cache_clear()


def test_bad_image_raises_image_error():
    with pytest.raises(vision.ImageError):
        extract.extract_text(b"not an image")
