"""Field filter (fields.py): only manufacturer / batch / expiry / licence survive; never guessed; register match through the indexed key."""
import pytest

import db
import extract
import fields

# Tesseract output for a real blister strip (Istavel-D): no batch, no expiry, no code
STRIP = """⚠ Staglotn Tk’
Each film coated tablet contains:
Dapaglifiozin Propanediol Monohydrate
equivalent to Sitagliptin 50 mg
Store at temperature not exceeding
30°C, protected from moisture.
PHARMA
Mfg. Lic. No.: 374/DR/Mfg/2013
Manufactured in India by:
sun pharma laboratories Itd.
5231806
Vill: Kokjhar, Mirza Palashbari Road,
® Registered Trade Mark"""


def test_real_strip_keeps_manufacturer_and_licence_and_invents_nothing():
    f = fields.filter_text(STRIP)
    assert f.manufacturer == "sun pharma laboratories Itd."
    assert f.licence == "374/DR/Mfg/2013"
    assert f.batch is None and f.expiry is None            # the vertical 5231806 is not labelled a batch: never guessed


def test_printed_label_batch_expiry_manufacturer():
    f = fields.filter_text("CholeZen 600\nMfd. by: Zenith Labs Pvt. Ltd.\nBatch No.: GH6625\nExp. 01/2030\nSAMPLE PACK")
    assert (f.manufacturer, f.batch, f.expiry) == ("Zenith Labs Pvt. Ltd.", "GH6625", (2030, 1))


def test_florence_style_typos_and_double_colon():
    f = fields.filter_text("Mdf. by: Zenith Labs Pvt. Ltd.\nBatch No: : GH6625\nExp : 01/2030")
    assert (f.manufacturer, f.batch, f.expiry) == ("Zenith Labs Pvt. Ltd.", "GH6625", (2030, 1))


def test_batch_word_without_a_number_is_not_a_batch():
    assert fields.filter_text("Batch No. and expiry are printed on the carton").batch is None


def test_low_confidence_lines_are_dropped():
    f = fields.filter_text(f"{extract.UNCLEAR}Batch No.: AX2291\n{extract.UNCLEAR}Exp. 03/2028")
    assert not f.any()


def test_code_payload_beats_printed_text():
    f = fields.filter_text("Batch No.: WRONG1\nExp. 01/2020\n[QRCode] (01)08900523898688(10)GH6625(17)300119(21)SN1")
    assert (f.batch, f.expiry) == ("GH6625", (2030, 1))


def test_nothing_relevant_means_no_fields():
    assert not fields.filter_text("Store in a cool dry place\nKeep out of reach of children").any()


def test_manufacturer_is_length_capped():
    assert len(fields.filter_text("Mfd. by: " + "Alpha " * 40 + "Pvt. Ltd.").manufacturer) <= fields.MAX_LEN


# ---------------- register match (needs the test database) ----------------
def _batch():
    return db.one("SELECT b.batch_number, p.brand_name FROM batches b JOIN products p USING (product_code) WHERE b.batch_number ~ '^[A-Z]{2}[0-9]{4}$' LIMIT 1")


@pytest.mark.parametrize("mangle", [lambda b: b, lambda b: b.lower(), lambda b: b[:2] + "-" + b[2:], lambda b: b[:2] + " " + b[2:]])
def test_register_match_ignores_case_and_punctuation(mangle):
    r = _batch()
    assert fields.match_register(mangle(r["batch_number"]))["batch_number"] == r["batch_number"]


def test_register_match_tolerates_ocr_letter_digit_swaps():
    row = db.one("SELECT batch_number FROM batches WHERE batch_number ~ '[0OIL1]' AND batch_number ~ '^[A-Z0-9]+$' LIMIT 1")
    swapped = row["batch_number"].translate(str.maketrans("0O1IL", "O0LL1"))
    hit = fields.match_register(swapped)
    assert hit is None or hit["batch_number"] == row["batch_number"]        # found, or ambiguous -> None; never a different batch
    assert fields.match_register(row["batch_number"])["batch_number"] == row["batch_number"]


def test_register_match_unknown_or_empty_is_none():
    assert fields.match_register("ZZZZ9999") is None and fields.match_register("--") is None


def test_ocr_swap_spellings_are_bounded():
    assert len(fields._keys("OOOOOOOOOOOO")) == 1              # 2^12 spellings would exceed the cap: exact key only
    assert set(fields._keys("A0")) == {"A0", "AO"}


def test_batch_key_is_indexed_and_matches_generated_value():
    r = db.one("SELECT batch_number, batch_key FROM batches LIMIT 1")
    assert r["batch_key"] == "".join(ch for ch in r["batch_number"].upper() if ch.isalnum())
    assert db.one("SELECT 1 FROM pg_indexes WHERE indexname IN ('idx_batches_batch_key')")
