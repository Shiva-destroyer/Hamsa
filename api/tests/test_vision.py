"""Photo path: GS1 parsing, QR/DataMatrix decoding, EXIF/image rejection, blur / low-res / crop / missing-field heuristics,
label-vs-code (manufacturer via GTIN, batch, expiry). Fixtures are generated here (no dependency on assets/packs except the 5-pack acceptance test).
Needs the seeded DB (DATABASE_URL) for the end-to-end tests."""
import io
import os
from datetime import date

import cv2
import numpy as np
import pytest
import zxingcpp
from PIL import Image, ImageDraw, ImageFont

import db
import engine
import vision

PACKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "packs")
GS = "\x1d"


# ---------------------------------------------------------------------------------------------
# fixture generators
# ---------------------------------------------------------------------------------------------
def _font(sz):
    for f in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(f, sz)
        except Exception:
            pass
    return ImageFont.load_default()


def render_pack(code_text, sym="qr", mfr="Cipla Ltd.", batch="AX2291", exp="03/2028", show_fields=True, W=1000, H=1450):
    """A printed-pack look-alike: label lines + a QR ('qr') or DataMatrix ('dm') carrying `code_text`."""
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([20, 20, W - 20, H - 20], outline="black", width=6)
    d.text((60, 60), "Testomol 500", fill="black", font=_font(80))
    d.text((60, 170), "Paracetamol Tablets IP", fill="black", font=_font(40))
    sz = 46
    while sz > 20 and d.textlength("Mfd. by: " + mfr, font=_font(sz)) > W - 140:     # long names must fit inside the frame, not run off it
        sz -= 2
    d.text((60, 300), "Mfd. by: " + mfr, fill="black", font=_font(sz))
    if show_fields:
        d.text((60, 390), f"Batch No.: {batch}", fill="black", font=_font(56))
        d.text((60, 480), f"Exp.: {exp}", fill="black", font=_font(56))
    fmt = zxingcpp.BarcodeFormat.QRCode if sym == "qr" else zxingcpp.BarcodeFormat.DataMatrix
    code = np.array(zxingcpp.write_barcode(fmt, code_text, 0, 0, quiet_zone=40))
    k = max(1, 600 // max(code.shape))
    code = np.kron(code, np.ones((k, k), np.uint8))
    im.paste(Image.fromarray(code).convert("RGB"), ((W - code.shape[1]) // 2, 700))
    d.text((60, 1380), "SAMPLE PACK - HAMSA DEMO - NOT A REAL MEDICINE", fill="black", font=_font(28))
    return im


def blur(im, sigma):
    return Image.fromarray(cv2.GaussianBlur(np.asarray(im), (0, 0), sigma))


def to_bytes(im, fmt="PNG", **kw):
    b = io.BytesIO()
    im.save(b, format=fmt, **kw)
    return b.getvalue()


def gs1(gtin, batch, exp, serial="SN0001", raw=False):
    if raw:
        return f"01{gtin.zfill(14)}10{batch}{GS}17{exp}{GS}21{serial}"
    return f"(01){gtin.zfill(14)}(10){batch}(17){exp}(21){serial}"


@pytest.fixture(scope="module")
def reg():
    """A registered, in-date, NSQ-free batch from the seed: MQ7756 (GREEN on manual entry)."""
    r = db.one("""SELECT b.batch_number, b.expiry_date, p.product_code, p.manufacturer
                  FROM batches b JOIN products p USING (product_code) WHERE b.batch_number = 'MQ7756'""")
    return dict(r, exp_yymmdd=f"{r['expiry_date']:%y%m%d}", exp_print=f"{r['expiry_date']:%m/%Y}")


def pack_for(reg, **kw):
    """Pack for the registered batch; keyword args override rendering (mfr=, batch=, exp=, sym=, show_fields=, raw=, code_text=)."""
    raw = kw.pop("raw", False)
    code = kw.pop("code_text", None) or gs1(reg["product_code"], reg["batch_number"], reg["exp_yymmdd"], raw=raw)
    args = dict(mfr=reg["manufacturer"], batch=reg["batch_number"], exp=reg["exp_print"])
    args.update(kw)
    return render_pack(code, **args)


def bgr_of(im):
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


# ---------------------------------------------------------------------------------------------
# GS1 parsing
# ---------------------------------------------------------------------------------------------
G = "08907573869302"


def test_gs1_parenthesised():
    f = vision.parse_gs1(f"(01){G}(10)AX2291(17)280331(21)SN0001")
    assert f == {"gtin": G.lstrip("0"), "batch": "AX2291", "expiry": date(2028, 3, 31), "serial": "SN0001"}


def test_gs1_raw_with_gs_separator_and_serial_last():
    f = vision.parse_gs1(f"01{G}10AX2291{GS}17280331{GS}21SN0001")
    assert (f["gtin"], f["batch"], f["expiry"], f["serial"]) == (G.lstrip("0"), "AX2291", date(2028, 3, 31), "SN0001")


def test_gs1_raw_fixed_length_ai_needs_no_separator_after_it():
    # 17 is fixed length, so batch(10) first + GS, then 17 + 21 with no GS between them is valid GS1
    f = vision.parse_gs1(f"01{G}10AX2291{GS}17280331" + "21SN9")
    assert f["serial"] == "SN9" and f["expiry"] == date(2028, 3, 31)


def test_gs1_raw_fixed_first_ordering():
    f = vision.parse_gs1(f"01{G}17280331" + "10AX2291" + GS + "21SN9")
    assert f["batch"] == "AX2291" and f["serial"] == "SN9"


@pytest.mark.parametrize("prefix", ["]d2", "]Q3", ""])
def test_gs1_symbology_identifier_stripped(prefix):
    assert vision.parse_gs1(prefix + f"01{G}10AX2291{GS}17280331")["batch"] == "AX2291"


def test_gs1_hri_gs_marker_and_lowercase_batch():
    assert vision.parse_gs1(f"01{G}10ax2291<GS>17280331")["batch"] == "AX2291"


def test_gs1_day_zero_is_end_of_month():
    assert vision.parse_gs1(f"(01){G}(10)AX2291(17)280200")["expiry"] == date(2028, 2, 29)


def test_gs1_13_digit_gtin_parenthesised_kept():
    f = vision.parse_gs1("(01)8907573869302(10)AX2291(17)280331")
    assert f["gtin"] == "8907573869302" and f["serial"] == ""


@pytest.mark.parametrize("bad", [
    None, "", "   ", "hello world", "https://example.com/verify?b=AX2291",
    f"(01){G}(17)280331",                               # no batch
    f"(01){G}(10)AX2291",                               # no expiry
    f"(10)AX2291(17)280331",                            # no GTIN
    f"(01){G}(10)AX2291(17)281331",                     # month 13
    f"(01){G}(10)AX2291(17)280231",                     # 31 Feb
    f"(01){G}(10)AX2291(17)28033",                      # short date
    f"(01)ABCDEFGHIJKLMN(10)AX2291(17)280331",          # non-numeric GTIN
    f"(01)12345(10)AX2291(17)280331",                   # short GTIN
    f"(01){G}(10)AX2291(10)AX2292(17)280331",           # duplicate AI
    f"(01){G}(10)(17)280331",                           # empty batch
    f"(01){G}(10)AX2291(17)280331(21",                  # truncated
    f"(01{G}10AX2291",                                  # unbalanced parenthesis
    f"01{G}10AX2291{GS}17",                             # raw truncated expiry
    f"01{G}10{'A' * 21}{GS}17280331",                   # batch over 20 chars
    f"01{G}10AX2291{GS}17280A31",                       # non-digit in expiry
    f"01{G}99",                                         # nothing usable
    f"77{G}10AX2291{GS}17280331",                       # unknown AI in raw form
    f"(01){G}(10)AX2291;DROP TABLE x(17)280331",        # illegal batch characters
])
def test_gs1_malformed_returns_none_never_raises(bad):
    assert vision.parse_gs1(bad) is None


# ---------------------------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sym", ["qr", "dm"])
@pytest.mark.parametrize("raw", [False, True], ids=["parenthesised", "raw_gs"])
def test_decode_qr_and_datamatrix_both_gs1_syntaxes(sym, raw):
    text = gs1(G, "AX2291", "280331", raw=raw)
    codes = vision.decode_codes(bgr_of(render_pack(text, sym=sym)))
    assert codes and codes[0].fmt == ("QRCode" if sym == "qr" else "DataMatrix")
    assert vision.parse_gs1(codes[0].text)["batch"] == "AX2291"


def test_opencv_qr_fallback_when_zxing_missing(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "zxingcpp", None)      # `import zxingcpp` -> ImportError
    codes = vision.decode_codes(bgr_of(render_pack(gs1(G, "AX2291", "280331"), sym="qr")))
    assert codes and vision.parse_gs1(codes[0].text)["batch"] == "AX2291"


def test_blank_image_decodes_nothing():
    assert vision.decode_codes(bgr_of(Image.new("RGB", (800, 800), "white"))) == []


# ---------------------------------------------------------------------------------------------
# image loading / rejection / EXIF
# ---------------------------------------------------------------------------------------------
def test_strip_exif_removes_gps_and_applies_orientation():
    im = Image.new("RGB", (300, 200), "white")
    ImageDraw.Draw(im).rectangle([0, 0, 50, 50], fill="red")           # marker in the top-left corner
    ex = Image.Exif()
    ex[0x0112] = 6                                                     # Orientation: rotate 90 CW on display
    ex[0x010F] = "SecretCam"
    gps = ex.get_ifd(0x8825)
    gps[2] = (12.0, 34.0, 56.0)
    jpg = to_bytes(im, "JPEG", exif=ex)
    assert Image.open(io.BytesIO(jpg)).getexif()                      # the input really has EXIF
    out = Image.open(io.BytesIO(vision.strip_exif(jpg)))
    assert not out.getexif() and "exif" not in out.info
    assert out.size == (200, 300)                                     # orientation applied before EXIF was dropped


@pytest.mark.parametrize("mk", [
    lambda: b"",
    lambda: b"not an image at all",
    lambda: b"\x89PNG\r\n\x1a\n" + b"\x00" * 20,                        # truncated PNG
    lambda: to_bytes(Image.new("RGB", (50, 50)), "GIF"),               # a real image, but not JPEG/PNG/WebP
    lambda: to_bytes(Image.new("RGB", (50, 50)), "BMP"),
    lambda: b"%PDF-1.4 fake",
])
def test_invalid_or_unsupported_images_rejected(mk):
    with pytest.raises(vision.ImageError):
        vision.verify_photo(mk())
    with pytest.raises(ValueError):                                     # ImageError is a ValueError (flow.py catches generically)
        vision.strip_exif(mk())


def test_oversize_bytes_rejected():
    with pytest.raises(vision.ImageError):
        vision.verify_photo(b"\x89PNG" + b"0" * (vision.MAX_IMAGE_BYTES + 1))


def test_pixel_bomb_rejected(monkeypatch):
    monkeypatch.setattr(vision, "MAX_PIXELS", 1000)
    with pytest.raises(vision.ImageError):
        vision.verify_photo(to_bytes(Image.new("RGB", (100, 100))))


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_supported_formats_accepted(fmt, reg):
    v, rec, notes = vision.verify_photo(to_bytes(pack_for(reg), fmt))
    assert v.verdict == "GREEN" and notes == []


def test_rgba_and_palette_images_accepted(reg):
    im = pack_for(reg)
    vision.verify_photo(to_bytes(im.convert("RGBA")))
    vision.verify_photo(to_bytes(im.convert("P")))


# ---------------------------------------------------------------------------------------------
# heuristics: blur / low-res / crop / missing fields (thresholds tuned on these generated fixtures)
#   sharpness (tile-percentile Laplacian variance): sharp render ~4700, JPEG q30 ~4000, 2x-upscaled render ~1400,
#   gaussian sigma 1.5 ~115, sigma 2 ~55, sigma 2.5 ~28, sigma 6 ~2.  BLUR_VAR_MIN = 100 flags sigma >= 2 ("mild blur").
# ---------------------------------------------------------------------------------------------
def test_sharp_photo_has_no_visual_signal(reg):
    v, rec, notes = vision.verify_photo(to_bytes(pack_for(reg)))
    assert notes == [] and "visual_heuristic" not in {s.signal_type for s in v.signals}
    assert v.verdict == "GREEN"


def test_jpeg_compression_is_not_blur(reg):
    v, _, notes = vision.verify_photo(to_bytes(pack_for(reg), "JPEG", quality=30))
    assert notes == []


def test_large_upscaled_photo_is_not_blur(reg):
    v, _, notes = vision.verify_photo(to_bytes(pack_for(reg).resize((2000, 2900), Image.BICUBIC)))
    assert notes == []


def test_mild_blur_decodes_and_triggers_blur_only(reg):
    im = blur(pack_for(reg), 2.5)
    assert vision.decode_codes(bgr_of(im))                           # the code still decodes
    v, rec, notes = vision.verify_photo(to_bytes(im))
    assert notes == ["BLUR"]                                         # ...and only BLUR fires (OCR still confident, fields present, not cropped)
    vis = next(s for s in v.signals if s.signal_type == "visual_heuristic")
    assert vis.status == "FAIL" and vis.confidence == "low"
    assert next(s for s in v.signals if s.signal_type == "code_validity").status == "PASS"
    assert v.verdict == "AMBER" and v.reason == "single_signal"


def test_heavy_blur_is_amber_never_red_from_upstream_symptoms(reg):
    v, _, notes = vision.verify_photo(to_bytes(blur(pack_for(reg), 6)))
    assert "BLUR" in notes and v.verdict == "AMBER"                  # blur + unreadable OCR are one cause (rule 3 independence)


def test_low_resolution_flagged_as_blur(reg):
    im = pack_for(reg).resize((500, 725), Image.LANCZOS)
    v, _, notes = vision.verify_photo(to_bytes(im))
    assert notes == ["BLUR"]
    assert "resolution" in next(s for s in v.signals if s.signal_type == "visual_heuristic").evidence_text


def test_599_vs_600_short_side_boundary():
    sharp = bgr_of(render_pack(gs1(G, "AX2291", "280331")))
    p = vision.Printed(has_batch_field=True)
    assert vision.visual_signal(sharp, (600, 900), [], [], p)[1] == []
    assert vision.visual_signal(sharp, (599, 900), [], [], p)[1] == ["BLUR"]


def test_crop_label_cut_at_top_edge(reg):
    v, _, notes = vision.verify_photo(to_bytes(pack_for(reg).crop((0, 320, 1000, 1450))))   # first label lines cut off
    assert "CROP" in notes and v.verdict == "AMBER"


def test_crop_code_touching_edge():
    im = render_pack(gs1(G, "AX2291", "280331"))
    x0, y0, x1, y1 = vision.decode_codes(bgr_of(im))[0].box
    tight = im.crop((int(x0) - 2, int(y0) - 2, int(x1) + 3, int(y1) + 3))
    b = bgr_of(tight)
    codes = vision.decode_codes(b)
    assert codes                                                       # still decodable...
    sig, notes = vision.visual_signal(b, tight.size, [c.box for c in codes], [], vision.Printed(has_batch_field=True))
    assert "CROP" in notes and sig.status == "FAIL"                    # ...but its bounding box touches the edge


def test_code_cut_off_is_qr_fail_with_crop(reg):
    v, rec, notes = vision.verify_photo(to_bytes(pack_for(reg).crop((330, 0, 1000, 1450))))
    assert notes[0] == "QR_FAIL" and "CROP" in notes and rec is None


def test_missing_print_fields(reg):
    v, _, notes = vision.verify_photo(to_bytes(pack_for(reg, show_fields=False)))
    assert notes == ["MISSING_FIELDS"]
    assert v.verdict == "AMBER"


def test_blank_page_is_qr_fail(reg):
    v, rec, notes = vision.verify_photo(to_bytes(Image.new("RGB", (1000, 1450), "white")))
    assert notes[0] == "QR_FAIL" and rec is None and v.verdict == "AMBER"


def test_non_gs1_qr_is_qr_fail():
    code = np.array(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, "https://example.com/v?b=AX2291", 400, 400))
    v, rec, notes = vision.verify_photo(to_bytes(Image.fromarray(code).convert("RGB")))
    assert notes[0] == "QR_FAIL" and rec is None
    assert next(s for s in v.signals if s.signal_type == "code_validity").source_reference == "Decoded payload"


# ---------------------------------------------------------------------------------------------
# label_consistency: manufacturer via GTIN, batch and expiry printed vs decoded
# ---------------------------------------------------------------------------------------------
def label(v):
    return next(s for s in v.signals if s.signal_type == "label_consistency")


@pytest.mark.parametrize("mfr_variant", [lambda m: m.upper(), lambda m: "M/s " + m, lambda m: m.replace("Ltd.", "Limited")])
def test_manufacturer_alias_variants_pass(reg, mfr_variant):
    v, _, notes = vision.verify_photo(to_bytes(pack_for(reg, mfr=mfr_variant(reg["manufacturer"]))))
    assert label(v).status == "PASS" and v.verdict == "GREEN", label(v).evidence_text


def test_manufacturer_resolved_via_gtin_not_batch_register(reg):
    # batch ZZ1234 is not in the register; the GTIN alone must supply the manufacturer
    assert engine.batch_record("ZZ1234") is None
    im = pack_for(reg, code_text=gs1(reg["product_code"], "ZZ1234", reg["exp_yymmdd"]), batch="ZZ1234")
    v, rec, notes = vision.verify_photo(to_bytes(im))
    assert label(v).status == "PASS" and "OCR_LOW_CONF" not in notes
    assert rec["batch_number"] == "ZZ1234" and rec["product_code"] == reg["product_code"]


def test_unknown_gtin_is_inconclusive_not_guessed(reg):
    # unregistered batch too, so neither the GTIN nor the batch register can supply a manufacturer
    im = pack_for(reg, code_text=gs1("8900000000005", "ZZ1234", reg["exp_yymmdd"]), batch="ZZ1234")
    v, rec, notes = vision.verify_photo(to_bytes(im))
    assert label(v).status == "INCONCLUSIVE" and "OCR_LOW_CONF" in notes


def test_manufacturer_mismatch_fails_label_only_amber(reg):
    v, _, _ = vision.verify_photo(to_bytes(pack_for(reg, mfr="Zenith Labs Pvt. Ltd.")))
    assert label(v).status == "FAIL" and v.verdict == "AMBER" and v.reason == "single_signal"


def test_printed_batch_differs_from_code_fails(reg):
    v, _, _ = vision.verify_photo(to_bytes(pack_for(reg, batch="MQ7759")))
    s = label(v)
    assert s.status == "FAIL" and "MQ7759" in s.evidence_text and reg["batch_number"] in s.evidence_text
    assert v.verdict == "AMBER"


def test_printed_expiry_differs_from_code_fails(reg):
    v, _, _ = vision.verify_photo(to_bytes(pack_for(reg, exp="01/2031")))
    s = label(v)
    assert s.status == "FAIL" and "expiry" in s.evidence_text and v.verdict == "AMBER"


def test_ocr_lookalike_batch_characters_are_not_a_mismatch(reg):
    # OCR classically swaps O/0 and I/1; the canonical comparison must not turn that into a false alarm
    assert vision._canon_batch("MQ775O") == vision._canon_batch("MQ7750")
    assert vision._canon_batch("AX22I1") == vision._canon_batch("AX2211")
    assert vision._canon_batch("AX2291") != vision._canon_batch("AX2299")


@pytest.mark.parametrize("text,ym", [("03/2028", (2028, 3)), ("3-28", (2028, 3)), ("03.2028", (2028, 3)), ("MAR 2028", (2028, 3)),
                                     ("Mar-2028", (2028, 3)), ("12/03/2028", (2028, 3)), ("13/2028", None), ("hello", None)])
def test_expiry_text_parsing(text, ym):
    assert vision._parse_month_year(text) == ym


def test_label_mismatch_plus_replay_flag_escalates_and_serial_is_passed(reg, monkeypatch):
    seen = {}
    real = engine.replay_signal

    def spy(batch, serial=None):
        seen["args"] = (batch, serial)
        return engine.Signal("replay_pattern", "FLAGGED", "[Preview] x", "scan_log", "s", "low", synthetic=True)

    monkeypatch.setattr(engine, "replay_signal", spy)
    v, _, _ = vision.verify_photo(to_bytes(pack_for(reg, mfr="Zenith Labs Pvt. Ltd.")))
    assert seen["args"] == (reg["batch_number"], "SN0001")            # the decoded serial reaches the replay preview
    assert (v.verdict, v.reason) == ("RED", "escalated_conflict")
    assert real is not spy


def test_visual_signal_never_escalates_with_label_mismatch(reg):
    im = blur(pack_for(reg, mfr="Zenith Labs Pvt. Ltd."), 2.5)
    v, _, notes = vision.verify_photo(to_bytes(im))
    kinds = {(s.signal_type, s.status) for s in v.signals}
    assert ("visual_heuristic", "FAIL") in kinds
    assert v.verdict == "AMBER"                                       # blur is upstream of label_consistency -> not an independent second signal


def test_qr_payload_verify_has_no_label_row(reg):
    v, rec, notes = vision.verify_qr_payload(gs1(reg["product_code"], reg["batch_number"], reg["exp_yymmdd"]))
    assert v.verdict == "GREEN" and notes == []
    assert {s.signal_type for s in v.signals} == {"code_validity", "nsq_status", "expiry_status", "replay_pattern"}
    v, rec, notes = vision.verify_qr_payload("garbage")
    assert v.verdict == "AMBER" and notes == ["QR_FAIL"] and rec is None


# ---------------------------------------------------------------------------------------------
# acceptance: the 5 shipped demo packs
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("batch,verdict,reason", [
    ("AX2291", "RED", "nsq_match"), ("EN3302", "EXPIRED", "expired"), ("MQ7756", "GREEN", "no_warning"),
    ("GH6625", "AMBER", "single_signal"), ("CT5510", "RED", "escalated_conflict")])
def test_five_shipped_packs(batch, verdict, reason):
    v, rec, notes = vision.verify_photo(open(os.path.join(PACKS, f"{batch}.png"), "rb").read())
    assert (v.verdict, v.reason) == (verdict, reason), [(s.signal_type, s.status) for s in v.signals]
    assert rec["batch_number"] == batch
