"""Pre-generates ALL voice notes once (5 verdict keys x en/hi/kn = 15 files) so the demo has ZERO text-to-speech
dependency at runtime.  Needs internet + the ffmpeg binary, once.

    python scripts/gen_voice.py                 -> assets/voice/<KEY>_<lang>.ogg + MANIFEST.json
    python scripts/gen_voice.py --list-voices   -> show available en-IN / hi-IN / kn-IN voices
    python scripts/gen_voice.py --verify        -> ffprobe every file + DRY_RUN wa.upload_media; prints "15/15 OK"

Engine: edge-tts first; per-file fallback to gTTS if edge-tts fails (the engine used is recorded in MANIFEST.json).
Text comes from texts.VERDICT[key]["action"][lang] at run time. After ANY correction to texts.py (language
review), re-run this script: --verify reports files whose manifest text no longer matches texts.py as STALE.

Format matters: WhatsApp only shows a native voice note for OGG + OPUS, and only shows the play icon if the file
is <= 512 KB. These clips are ~10-20 KB.
"""
import asyncio, json, os, subprocess, sys, tempfile
API = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, API)
from texts import VERDICT

OUT = os.path.join(API, "assets", "voice")
MANIFEST = os.path.join(OUT, "MANIFEST.json")
# Confirmed present in `edge-tts --list-voices`.
VOICES = {"en": "en-IN-NeerjaNeural", "hi": "hi-IN-SwaraNeural", "kn": "kn-IN-SapnaNeural"}
GTTS = {"en": ("en", "co.in"), "hi": ("hi", "co.in"), "kn": ("kn", "co.in")}
KEYS = ["RED_nsq", "RED_escalated", "EXPIRED", "AMBER", "GREEN"]
MAX_BYTES, MIN_S, MAX_S = 512 * 1024, 2.0, 20.0


def _probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=codec_name,channels:format=duration,size", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    st = (j.get("streams") or [{}])[0]
    return st.get("codec_name"), st.get("channels"), float(j["format"]["duration"]), int(j["format"]["size"])


async def _edge(text, voice, mp3):
    import edge_tts
    await edge_tts.Communicate(text, voice).save(mp3)


def _gtts(text, lang, mp3):
    from gtts import gTTS
    code, tld = GTTS[lang]
    gTTS(text, lang=code, tld=tld).save(mp3)


def _to_opus(src, ogg):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c:a", "libopus", "-b:a", "24k",
                    "-ac", "1", "-ar", "24000", "-application", "voip", ogg], check=True)


async def generate():
    os.makedirs(OUT, exist_ok=True)
    entries = []
    with tempfile.TemporaryDirectory() as tmp:
        for key in KEYS:
            for lang, voice in VOICES.items():
                text = VERDICT[key]["action"][lang]
                mp3, ogg = os.path.join(tmp, f"{key}_{lang}.mp3"), os.path.join(OUT, f"{key}_{lang}.ogg")
                engine, used_voice = "edge-tts", voice
                try:
                    await _edge(text, voice, mp3)
                except Exception as e:                       # blocked / offline / voice missing -> gTTS fallback
                    print(f"[warn] edge-tts failed for {key}_{lang} ({type(e).__name__}: {e}); falling back to gTTS")
                    _gtts(text, lang, mp3)
                    engine, used_voice = "gTTS", f"gTTS-{GTTS[lang][0]}-{GTTS[lang][1]}"
                _to_opus(mp3, ogg)
                _, _, dur, size = _probe(ogg)
                entries.append({"key": key, "lang": lang, "text": text, "engine": engine, "voice": used_voice,
                                "duration_s": round(dur, 2), "bytes": size})
                print(f"{ogg}  {size // 1024} KB  {dur:.1f}s  [{engine}]")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print("wrote", MANIFEST)


def verify():
    os.environ["DRY_RUN"] = "1"                              # no network: wa.upload_media returns a fake id
    fails = []
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            man = {(e["key"], e["lang"]): e for e in json.load(f)}
    except Exception as e:
        print(f"FAIL: cannot read {MANIFEST}: {e}"); sys.exit(1)
    import wa
    ok = 0
    for key in KEYS:
        for lang in VOICES:
            name, path = f"{key}_{lang}.ogg", os.path.join(OUT, f"{key}_{lang}.ogg")
            errs = []
            m = man.get((key, lang))
            if not m:
                errs.append("not in MANIFEST")
            elif m["text"] != VERDICT[key]["action"][lang]:
                errs.append("STALE: texts.py changed since generation, regenerate")
            if not os.path.exists(path):
                errs.append("file missing")
            else:
                try:
                    codec, ch, dur, size = _probe(path)
                    if codec != "opus": errs.append(f"codec={codec}")
                    if ch != 1: errs.append(f"channels={ch}")
                    if size > MAX_BYTES: errs.append(f"size={size}")
                    if not MIN_S <= dur <= MAX_S: errs.append(f"duration={dur:.1f}s")
                    if m and (m["bytes"] != size): errs.append("manifest bytes mismatch")
                    if not str(wa.upload_media(path, "audio/ogg; codecs=opus")).startswith("dry-media-"):
                        errs.append("DRY_RUN upload_media failed")
                except Exception as e:
                    errs.append(f"ffprobe error: {e}")
            if errs: fails.append(f"{name}: " + "; ".join(errs))
            else: ok += 1
    total = len(KEYS) * len(VOICES)
    if fails:
        print(f"{ok}/{total} OK"); [print("  FAIL", x) for x in fails]; sys.exit(1)
    print(f"{ok}/{total} OK")


if __name__ == "__main__":
    if "--list-voices" in sys.argv:
        os.system("edge-tts --list-voices | grep -E 'hi-IN|kn-IN|en-IN'")
    elif "--verify" in sys.argv:
        verify()
    else:
        asyncio.run(generate())
