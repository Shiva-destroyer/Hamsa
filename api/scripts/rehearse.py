"""Plays the whole demo script in-process, in English, Hindi and Kannada, asserting the phrases and the number/order of
outbound messages at every beat. No network, no phone. Uses DATABASE_URL (a scratch DB is fine) and DEMO_REAL_BATCH if set.

    DATABASE_URL=... python scripts/rehearse.py          -> prints "ALL BEATS PASS (en/hi/kn)", exit 0
Beat 7 (regulator overturn + TIMELINE) needs the running API and is not run here: use scripts/regulator_demo.py live."""
import functools, os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from harness import Chat, read
import db, engine
from texts import t, VERDICT
import make_demo_packs as packs

VERDICT_MSGS = ["text", "audio", "interactive"]        # structured text, native voice note, next-step buttons
failures = []


def beat(lang, name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  [{lang}] {name}" + ("" if ok else f"  -> {detail}"))
    if not ok: failures.append(f"[{lang}] {name}: {detail}")


@functools.lru_cache(maxsize=1)
def real_batch():
    b = os.environ.get("DEMO_REAL_BATCH", "").strip().upper()
    if re.fullmatch(r"[A-Z0-9][A-Z0-9\-/]{3,19}", b):       # junk (e.g. an inline .env comment parsed as the value) counts as unset
        try:
            if engine.verify_manual(b)[0].verdict == "RED": return b, True
        except Exception:
            pass
        print(f"  note: DEMO_REAL_BATCH={b} is not RED in this DB -- falling back to AX2291")
    else:
        print("  note: DEMO_REAL_BATCH not set -- beat 1 uses the synthetic AX2291")
    return "AX2291", False


def run_lang(lang, pack_dir):
    c = Chat("9195" + str(abs(hash(lang)) % 10**7).zfill(7) + "1")
    c.wipe()
    heads = {k: v["head"][lang] for k, v in VERDICT.items()}
    try:
        # 0 consent + language: nothing is processed before consent
        c.text("hi");           beat(lang, "0 consent first", "Do you agree" in c.said and c.types == ["interactive"], c.said[:60])
        c.text("AX2291");       beat(lang, "0 no processing before consent", "Do you agree" in c.said and "RED" not in c.said)
        c.button("agree");      beat(lang, "0 language buttons", c.types == ["interactive"] and "English" in str(c.out))
        c.button("lang_" + lang); beat(lang, "0 menu", c.said == t("menu", lang))

        # 1 real NSQ match (fallback AX2291) + WHY
        batch, real = real_batch()
        c.text(batch)
        ok = c.types == VERDICT_MSGS and heads["RED_nsq"] in c.said and "CDSCO" in c.said and c.voice == f"dry-media-RED_nsq_{lang}.ogg"
        if real: ok = ok and ".pdf" in c.said and "Demo dataset" not in c.said and t("demo_tag", lang) not in c.said
        beat(lang, f"1 RED {batch} ({'real' if real else 'synthetic'})", ok, f"{c.types} {c.said[:80]!r}")
        c.text("WHY");          beat(lang, "1 WHY provenance", "Confidence:" in c.said and "Source type" in c.said)

        # 2 expired is not red
        c.cooldown_off().text("EN3302")
        beat(lang, "2 EXPIRED", c.types == VERDICT_MSGS and heads["EXPIRED"] in c.said and heads["RED_nsq"] not in c.said, c.said[:80])

        # 3 label-vs-code mismatch: one weak signal never says RED
        c.cooldown_off().photo(read(f"{pack_dir}/GH6625.png"))
        beat(lang, "3 photo GH6625 -> AMBER", c.types == VERDICT_MSGS + ["text"] and heads["AMBER"] in c.said and "RED" not in c.said.split("\n")[0], c.said[:80])

        # 4 conflict escalation
        c.cooldown_off().photo(read(f"{pack_dir}/CT5510.png"))
        beat(lang, "4 photo CT5510 -> RED escalated", c.types == VERDICT_MSGS + ["text"] and heads["RED_escalated"] in c.said and t("preview_tag", lang) in c.said, c.said[:80])

        # 5 dispute: banner appears, verdict unchanged
        c.cooldown_off().text("AX2291")
        c.button("dispute");    beat(lang, "5 dispute asks email", c.said == t("dispute_email", lang, b="AX2291"))
        c.text("qa@amruthadrugs.com"); beat(lang, "5 domain verified", c.said == t("dispute_evid", lang))
        c.text("Corrected Certificate of Analysis attached"); beat(lang, "5 dispute filed", c.said == t("dispute_ok", lang, b="AX2291"))
        c.cooldown_off().text("AX2291")
        beat(lang, "5 Under review, verdict unchanged", heads["RED_nsq"] in c.said and t("dispute_banner", lang) in c.said, c.said[:80])

        # 6 voice note in the chosen language
        c.text("LANG");         beat(lang, "6 LANG offers buttons", c.types == ["interactive"])
        c.button("lang_" + lang); c.cooldown_off().text("AX2291")
        beat(lang, "6 voice note", c.voice == f"dry-media-RED_nsq_{lang}.ogg" and c.types == VERDICT_MSGS, str(c.voice))

        # extras that the Q&A tends to poke at
        c.text("TIMELINE AX2291"); beat(lang, "TIMELINE", "AX2291" in c.said and "CDSCO" in c.said)
        c.text("DELETE");       c.text("YES")
        beat(lang, "DELETE erases", c.session() is None and c.said == t("delete_done", lang))
    finally:
        c.wipe()


def main():
    tmp = tempfile.mkdtemp(prefix="rehearse-packs-")
    for n in ("GH6625", "CT5510"): packs.make(n, packs.PACKS[n], tmp)
    for lang in ("en", "hi", "kn"):
        print(f"--- {lang} ---"); run_lang(lang, tmp)
    disputes = db.one("SELECT count(*) n FROM disputes WHERE batch_number = 'AX2291' AND channel = 'whatsapp'")["n"]
    if disputes: failures.append(f"{disputes} leftover WhatsApp dispute(s) on AX2291")
    if failures:
        print("\nBEATS FAILED:\n  " + "\n  ".join(failures)); sys.exit(1)
    print("\nALL BEATS PASS (en/hi/kn)")


if __name__ == "__main__":
    main()
