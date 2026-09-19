"""Verdict message: short, plain, no report-style evidence block; every warning has a one-line reason; safety lines kept."""
import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import harness
import make_demo_packs as packs
from texts import S


@pytest.fixture
def chat(monkeypatch):
    c = harness.Chat(mp=monkeypatch); c.wipe(); yield c; c.wipe()


def first(chat):
    return chat.out[0]["text"]["body"]


def test_red_message_is_short_and_plain(chat):
    chat.onboard("en"); chat.text("AX2291"); m = first(chat)
    assert "CDSCO, the government drug regulator" in m and "Do not consume" in m
    assert "Evidence" not in m and "confidence:" not in m and "Inconclusive" not in m and "•" not in m
    assert len([l for l in m.splitlines() if l.strip()]) <= 9           # was ~35 lines with the evidence block


def test_blurred_photo_amber_explains_itself(chat, tmp_path):
    chat.onboard("en"); chat.photo(harness.read(packs.make("LP2098", packs.PACKS["LP2098"], str(tmp_path)))); m = first(chat)
    assert "AMBER" in m.splitlines()[0] and "blurry or cut off" in m


def test_label_mismatch_amber_explains_itself(chat, tmp_path):
    chat.onboard("en"); chat.photo(harness.read(packs.make("GH6625", packs.PACKS["GH6625"], str(tmp_path)))); m = first(chat)
    assert "AMBER" in m.splitlines()[0] and "label printed on the pack does not match" in m


def test_corrected_earlier_listing_stays_visible(chat):
    chat.onboard("en"); chat.text("RV5567")
    assert "earlier warning for this batch was corrected" in first(chat)


@pytest.mark.parametrize("key", [k for k in S if k.startswith(("r_", "c_")) or k == "disclaimer"])
def test_new_lines_exist_in_all_languages(key):
    assert all(S[key].get(lang) for lang in ("en", "hi", "kn"))


def test_no_emoji_in_any_bot_text():
    import re
    from texts import VERDICT
    emoji = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u23F3]")
    assert not [k for k, v in S.items() if any(emoji.search(x) for x in v.values())]
    assert not [k for k, v in VERDICT.items() if emoji.search(str(v))]


def test_real_nsq_record_is_not_tagged_as_demo_data(chat):
    """A real CDSCO row (data_origin='cdsco_real') with no register record must not carry the synthetic-demo tag; a synthetic one must."""
    import db
    db.run("DELETE FROM nsq_alerts WHERE batch_number = 'ZREAL9001'")
    db.run("""INSERT INTO nsq_alerts (id, product_name, batch_number, manufacturer, alert_date, reason, source_document, extraction_confidence, lab_type, status, data_origin)
              VALUES (gen_random_uuid(), 'Test Tablets', 'ZREAL9001', 'Real Pharma Pvt. Ltd.', '2025-04-30', 'Dissolution', 'CDSCO_TEST.pdf', 'medium', 'central', 'active', 'cdsco_real')""")
    try:
        chat.onboard("en"); chat.text("ZREAL9001"); real = first(chat)
        assert "RED" in real.splitlines()[0] and "CDSCO_TEST.pdf" in real
        assert "Demo dataset" not in real
        chat.wipe(); chat.onboard("en"); chat.text("AX2291")
        assert "Demo dataset" in first(chat)
    finally:
        db.run("DELETE FROM nsq_alerts WHERE batch_number = 'ZREAL9001'")
