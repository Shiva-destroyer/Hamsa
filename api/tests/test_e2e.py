"""scenario matrix end to end, through the WhatsApp flow (manual entry + photo). Needs the seeded DB with
demo_fixups.sql applied (DATABASE_URL), e.g. a scratch database. In-process; no network."""
import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import harness
import engine
import make_demo_packs as packs
from texts import t, VERDICT

VERDICT_MSGS = ["text", "audio", "interactive"]
# manual entry: batch -> (verdict key in texts.VERDICT, extra phrase expected in the text or None)
MANUAL = {
    "AX2291": ("RED_nsq", "Demo dataset"), "EN3302": ("EXPIRED", None), "MQ7756": ("GREEN", None), "NS3340": ("GREEN", None),
    "FM9184": ("RED_nsq", "Also expired"), "PT8813": ("RED_nsq", "Under review"), "RV5567": ("GREEN", None), "SW1123": ("GREEN", None),
    "TX6690": ("RED_nsq", None), "JK4471": ("AMBER", None), "CT5510": ("AMBER", None), "BQ7743": ("AMBER", None), "DR8820": ("AMBER", None),
    "GH6625": ("GREEN", None), "LP2098": ("GREEN", None)}
PHOTO_HEAD = {"RED": "RED_nsq", "EXPIRED": "EXPIRED", "GREEN": "GREEN", "AMBER": "AMBER"}


@pytest.fixture
def chat(monkeypatch):
    c = harness.Chat(mp=monkeypatch); c.wipe(); yield c; c.wipe()


@pytest.fixture(scope="module")
def pack_files(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("packs"))
    return {n: packs.make(n, spec, d) for n, spec in packs.PACKS.items()}


@pytest.mark.parametrize("batch", sorted(MANUAL))
def test_manual_matrix_via_whatsapp(chat, batch):
    key, phrase = MANUAL[batch]
    chat.onboard("en"); chat.text(batch)
    assert chat.types == VERDICT_MSGS, chat.said
    head = chat.said.split("\n")[0]
    assert VERDICT[key]["head"]["en"] in head, head
    if phrase: assert phrase in chat.said
    assert engine.verify_manual(batch)[0].verdict == VERDICT_KEY_TO_ENGINE[key]                 # the bot says what the engine says


VERDICT_KEY_TO_ENGINE = {"RED_nsq": "RED", "RED_escalated": "RED", "EXPIRED": "EXPIRED", "AMBER": "AMBER", "GREEN": "GREEN"}


@pytest.mark.parametrize("lang", ["en", "hi", "kn"])
@pytest.mark.parametrize("name", sorted(packs.PACKS))
def test_photo_matrix_via_whatsapp(chat, pack_files, name, lang):
    want_v, want_note = packs.PACKS[name]["expect"]
    chat.onboard(lang); chat.photo(harness.read(pack_files[name]))
    if want_note == "QR_FAIL":                                                                # malformed code -> type the batch number
        assert chat.types == ["text", "text"] and chat.out[-1]["text"]["body"] == t("qr_fail", lang)     # "text I read", then the manual-entry hint
        return
    assert chat.types == VERDICT_MSGS + ["text"], chat.said                                   # verdict, voice, buttons, then "text I read"
    key = "RED_escalated" if name in ("CT5510", "BQ7743") else PHOTO_HEAD[want_v]
    assert VERDICT[key]["head"][lang] in chat.said.split("\n")[0]
    assert chat.voice == f"dry-media-{key}_{lang}.ogg"
    if name in ("CT5510", "BQ7743"): assert t("preview_tag", lang) in chat.said                # replay row is labelled preview


def test_datamatrix_matches_its_batch_verdict(chat, pack_files):
    chat.onboard("en"); chat.photo(harness.read(pack_files["NS3340_DM"])); dm = chat.said.split("\n")[0]
    chat.cooldown_off().text("NS3340")
    assert dm == chat.said.split("\n")[0] and VERDICT["GREEN"]["head"]["en"] in dm


def test_green_never_claims_genuine(chat):
    chat.onboard("en"); chat.text("MQ7756")
    assert "does not confirm authenticity" in chat.said and "genuine" not in chat.said.replace("GREEN never means *genuine*", "")


def test_expired_is_not_red_and_red_can_also_be_expired(chat):
    chat.onboard("en"); chat.text("EN3302"); head = chat.said.split("\n")[0]
    assert "EXPIRED" in head and "RED —" not in head
    chat.cooldown_off().text("FM9184"); assert "RED —" in chat.said.split("\n")[0] and "Also expired" in chat.said
