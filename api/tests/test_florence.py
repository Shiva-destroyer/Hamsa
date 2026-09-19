"""Florence-2 as the primary photo-text reader, Tesseract as fallback. The suite runs with FLORENCE=0 (conftest); these tests fake the model
so they are offline and fast. The two real-model tests load ~470 MB of weights and only run with TEST_FLORENCE=1."""
import os
import sys

import pytest
from PIL import Image

import config
import extract
import florence
import vision
from test_vision import render_pack, to_bytes

CODE = "(01)08907573869302(10)AX2291(17)280331(21)SN0001"


def read(im):
    return extract.extract_text(to_bytes(im))


def no_tesseract(monkeypatch):
    def boom(*a, **k): raise AssertionError("Tesseract must not run when Florence answered")
    monkeypatch.setattr(vision, "ocr_words", boom)


def test_florence_text_is_used_first_with_code_payload(monkeypatch):
    monkeypatch.setattr(florence, "read_lines", lambda rgb: (["AmbroSafe 60", "Batch No: MQ7756"], 76.0))
    no_tesseract(monkeypatch)
    x = read(render_pack(CODE))
    assert x.engine == "florence" and x.ok and x.mean_conf == 76.0
    assert x.text.splitlines() == ["AmbroSafe 60", "Batch No: MQ7756", f"[QRCode] {CODE}"]


@pytest.mark.parametrize("answer", [None, ([], 90.0)], ids=["unavailable-or-not-confident", "found-nothing"])
def test_falls_back_to_tesseract(monkeypatch, answer):
    monkeypatch.setattr(florence, "read_lines", lambda rgb: answer)
    x = read(render_pack(CODE))
    assert x.engine == "tesseract" and "AX2291" in x.text


def test_disabled_by_config_wins_even_if_the_model_is_loaded(monkeypatch):
    monkeypatch.setattr(config, "FLORENCE", False)
    monkeypatch.setattr(florence, "_model", object())                     # pretend it is loaded: the switch must still turn it off
    assert florence.load() is False and florence.read_lines(Image.new("RGB", (50, 50), "white")) is None


def test_failed_load_is_remembered_and_falls_back(monkeypatch):
    monkeypatch.setattr(config, "FLORENCE", True)
    monkeypatch.setattr(florence, "_model", None)
    monkeypatch.setattr(florence, "_failed", False)
    monkeypatch.setitem(sys.modules, "transformers", None)               # import fails as if the package were missing
    assert florence.read_lines(Image.new("RGB", (50, 50), "white")) is None
    assert florence._failed is True and florence.load() is False          # remembered: no retry on every photo


real = pytest.mark.skipif(os.getenv("TEST_FLORENCE") != "1", reason="loads the real Florence-2 weights; set TEST_FLORENCE=1")


@real
def test_real_model_reads_the_pack(monkeypatch):
    monkeypatch.setattr(config, "FLORENCE", True)
    x = read(render_pack(CODE))
    assert x.engine == "florence" and x.mean_conf >= florence.MIN_CONF * 100
    assert "Testomol" in x.text and "AX2291" in x.text and f"[QRCode] {CODE}" in x.text


@real
def test_real_model_refuses_to_invent_text_on_blank_and_noise(monkeypatch):
    import numpy as np
    monkeypatch.setattr(config, "FLORENCE", True)
    noise = Image.fromarray(np.random.default_rng(0).integers(0, 255, (600, 800, 3), dtype=np.uint8))
    for im in (Image.new("RGB", (800, 600), "white"), noise):             # it hallucinates '0:00 PM' here; the confidence gate must refuse it
        assert florence.read_lines(im) is None
        assert read(im).engine == "tesseract"
