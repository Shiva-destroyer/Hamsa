"""WhatsApp conversation state machine.
States: NEW -> CONSENT -> LANG -> MENU <-> {AWAIT_EXPIRY | REPORT_TYPE -> REPORT_DESC | DISPUTE_EMAIL -> DISPUTE_EVIDENCE | DELETE_CONFIRM}
No batch number or photo is processed before consent is recorded (DPDP)."""
import calendar, json, os, re, threading
from datetime import date
import config, db, engine, extract, fields, log, privacy, snapshot, storage, timeline, vision, wa
from texts import t, VERDICT, verdict_key

IDLE_RESET_S = 24 * 3600         # session idle > 24 h -> treat as MENU
DISPUTE_DAILY_CAP = 10           # 10 disputes / day / domain
_locks, _locks_guard = {}, threading.Lock()

def _lock_for(ph):               # per-phone in-process lock: two quick messages can't interleave (single uvicorn worker)
    with _locks_guard:
        return _locks.setdefault(ph, threading.Lock())

BATCH_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-/]{3,19}$")
KEYWORDS = {"MENU", "HI", "HELLO", "START", "HELP", "DELETE", "DELETE MY DATA", "WHY", "LANG", "REPORT", "DISPUTE", "TIMELINE", "YES", "SKIP"}
ROW = {"nsq_status": {"en": "Official CDSCO NSQ record", "hi": "सरकारी CDSCO NSQ रिकॉर्ड", "kn": "ಅಧಿಕೃತ CDSCO NSQ ದಾಖಲೆ"},
       "expiry_status": {"en": "Expiry", "hi": "एक्सपायरी", "kn": "ಅವಧಿ ಮುಕ್ತಾಯ"},
       "label_consistency": {"en": "Label vs code", "hi": "लेबल बनाम कोड", "kn": "ಲೇಬಲ್ vs ಕೋಡ್"},
       "code_validity": {"en": "Code validity", "hi": "कोड की वैधता", "kn": "ಕೋಡ್ ಮಾನ್ಯತೆ"},
       "replay_pattern": {"en": "Replay/clone pattern [Preview]", "hi": "रीप्ले/क्लोन पैटर्न [प्रीव्यू]", "kn": "ರೀಪ್ಲೇ/ಕ್ಲೋನ್ ಮಾದರಿ [ಪ್ರೀವ್ಯೂ]"}}

# ---------- session helpers ----------
def _session(ph):
    db.run("INSERT INTO wa_sessions (phone_hash) VALUES (%s) ON CONFLICT DO NOTHING", (ph,))
    return db.one("""UPDATE wa_sessions s SET last_seen_at = now() FROM (SELECT phone_hash, last_seen_at AS prev FROM wa_sessions
                     WHERE phone_hash = %s) o WHERE s.phone_hash = o.phone_hash
                     RETURNING s.*, extract(epoch FROM now() - o.prev) AS idle_s""", (ph,))

def _set(ph, state=None, ctx=None, language=None):
    if state:    db.run("UPDATE wa_sessions SET state = %s WHERE phone_hash = %s", (state, ph))
    if language: db.run("UPDATE wa_sessions SET language = %s WHERE phone_hash = %s", (language, ph))
    if ctx is not None: db.run("UPDATE wa_sessions SET context = context || %s::jsonb WHERE phone_hash = %s", (json.dumps(ctx, default=str), ph))

def _claim(wamid):   # webhook idempotency: Meta retries; process each wamid exactly once
    return bool(db.one("INSERT INTO wa_inbound_dedupe (wamid) VALUES (%s) ON CONFLICT DO NOTHING RETURNING wamid", (wamid,)))

def _allowed_lookup(ph) -> bool:   # 1 verdict lookup / 10 s / number
    r = db.one("""UPDATE wa_sessions SET last_lookup_at = now() WHERE phone_hash = %s
                  AND (last_lookup_at IS NULL OR last_lookup_at < now() - make_interval(secs => %s)) RETURNING 1 AS ok""",
               (ph, config.LOOKUP_COOLDOWN_S))
    return bool(r)

# ---------- rendering ----------
_REASON = {"nsq_status": "r_nsq", "label_consistency": "r_label", "visual_heuristic": "r_visual", "code_validity": "r_code", "replay_pattern": "r_replay"}

def _why(v, lang):
    """(one plain sentence per warning, names of the checks that passed). Expiry is shown in the header, so it has no sentence.
    Per-check evidence, source and confidence stay behind WHY (provenance())."""
    bad, clear = [], []
    for s in v.signals:
        if s.signal_type not in ROW and s.signal_type not in _REASON or s.signal_type == "expiry_status" and s.status != "PASS": continue
        if s.status == "PASS":
            if s.signal_type == "nsq_status" and s.evidence_text.startswith("An earlier"): bad.append(t("r_nsq_fixed", lang))    # corrected listing stays visible
            clear.append(t("c_" + s.signal_type, lang))
        elif s.status in ("FAIL", "FLAGGED") and s.signal_type in _REASON:
            m = re.search(r"\((.+)\)$", s.evidence_text) if s.signal_type == "nsq_status" else None
            bad.append(t(_REASON[s.signal_type], lang, detail=f": {m.group(1)}" if m else ""))
        elif s.status == "INCONCLUSIVE" and s.signal_type in ("nsq_status", "label_consistency"):
            bad.append(t("r_nsq_unavail" if s.signal_type == "nsq_status" else "r_label_unsure", lang))
    return bad, clear

def render(v, rec, lang, checked, notes=()) -> str:
    V = VERDICT[verdict_key(v)]
    exp = (rec or {}).get("expiry_date")
    who = [x for x in ((rec or {}).get("brand_name"), f"{t('f_batch', lang)} {rec['batch_number']}" if (rec or {}).get("batch_number") else "",
                       f"{t('f_expiry', lang)} {exp:%m/%Y}" if hasattr(exp, "strftime") else "") if x]
    lines = [f"*{V['head'].get(lang) or V['head']['en']}*", " · ".join(who), "", V["action"].get(lang) or V["action"]["en"]]
    if v.dispute_open: lines += ["", t("dispute_banner", lang)]
    bad, clear = _why(v, lang)
    facts = ([t("r_esc", lang)] if v.reason == "escalated_conflict" else []) + ([t("r_expired", lang)] if v.also_expired and v.verdict == "RED" else []) + bad
    as_of = snapshot.get_snapshot_date() if any(s.from_cache for s in v.signals) else None
    if as_of: facts.append(t("r_asof", lang, date=f"{as_of:%d %b %Y}"))
    src = next((s.source_reference for s in v.signals if s.signal_type == "nsq_status" and s.status == "FAIL"), None)
    if src: facts.append(t("r_src", lang, ref=src))
    if facts: lines += [""] + facts
    if clear and v.verdict in ("GREEN", "AMBER"): lines += ["", t("r_clear", lang, names=", ".join(clear))]
    if any(k not in checked for k in ("code_validity", "label_consistency")): lines += ["", t("r_unchecked", lang)]
    tags = ([t("preview_tag", lang)] if any(s.signal_type == "replay_pattern" and s.status == "FLAGGED" for s in v.signals) else []) \
         + ([t("demo_tag", lang)] if "expiry_date" in (rec or {}) or any(s.synthetic and s.signal_type == "nsq_status" for s in v.signals) else [])
    return "\n".join(lines + ([""] + tags if tags else []) + ["", t("disclaimer", lang), t("footer", lang)])

def _serialize(v):
    return [dict(type=s.signal_type, status=s.status, src_type=s.source_type, ref=s.source_reference, conf=s.confidence,
                 updated=s.last_updated or "n/a", dispute=s.dispute) for s in v.signals]

def deliver_verdict(to, ph, lang, v, rec, checked, notes=()):
    batch = (rec or {}).get("batch_number", "")
    sid = engine.log_scan(v, batch, (rec or {}).get("product_code", ""))
    _set(ph, "MENU", {"last_scan_id": sid, "last_batch": batch, "prov": _serialize(v)})
    body = render(v, rec, lang, checked, notes)
    if v.reason == "data_unavailable" or snapshot.is_degraded(): body = t("degraded", lang) + "\n\n" + body
    log.event("verdict", who=ph[:8], verdict=v.verdict, reason=v.reason, batch=batch, lang=lang)
    wa.send_text(to, body)                                                                    # 1) structured text
    wa.send_voice_note(to, os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "voice", f"{verdict_key(v)}_{lang}.ogg"))                      # 2) same Safe Action as a voice note
    wa.send_buttons(to, t("next", lang), [("report", t("btn_report", lang)), ("again", t("btn_again", lang)), ("dispute", t("btn_dispute", lang))])  # 3) next steps

def provenance(ctx, lang) -> str:
    rows = ctx.get("prov") or []
    if not rows: return "No verdict yet — send a batch number first."
    out = ["*Provenance — where each fact came from*"]
    for r in rows:
        out += [f"• *{ROW.get(r['type'], {'en': r['type']})['en']}* — {r['status']}",
                f"   Source type: {r['src_type']} · Reference: {r['ref']}", f"   Confidence: {r['conf']} · Last updated: {r['updated']} · Dispute: {r['dispute']}"]
    return "\n".join(out)

# ---------- helpers ----------
def _parse_expiry(txt):
    m = re.fullmatch(r"(\d{1,2})[/\-](\d{4})", txt.strip())
    if not m: return None
    mo, yr = int(m.group(1)), int(m.group(2))
    return date(yr, mo, calendar.monthrange(yr, mo)[1]) if 1 <= mo <= 12 else None

def _lang_buttons(to):
    wa.send_buttons(to, t("lang_prompt"), [("lang_en", "English"), ("lang_hi", "हिन्दी"), ("lang_kn", "ಕನ್ನಡ")])

def _consent_prompt(to, lang="en"):
    wa.send_buttons(to, t("consent", lang), [("agree", t("btn_agree", lang)), ("decline", t("btn_decline", lang))])

# ---------- main entry ----------
def handle(m: wa.Inbound):
    """Never let an exception kill a live demo: log it and send a graceful fallback. One message per phone at a time."""
    ph = wa.identity_hash(m.sender)
    with _lock_for(ph):
        log.event("inbound", who=ph[:8], kind=m.kind, wamid=m.wamid)
        try:
            wa.mark_as_read(m.wamid)
            _handle(m)
        except Exception as e:
            log.event("handler_error", who=ph[:8], error=type(e).__name__, detail=str(e)[:200], level="error")
            try: wa.send_text(m.sender, t("error", _lang_of(ph)))
            except Exception: pass

def _lang_of(ph):
    try: return (db.one("SELECT language FROM wa_sessions WHERE phone_hash = %s", (ph,)) or {}).get("language") or "en"
    except Exception: return "en"

def _fetch(m):
    """Bytes of an inbound photo/document. DRY_RUN dev workflow (simulate_inbound.py): the caption is a file path."""
    if config.DRY_RUN and m.text and os.path.isfile(m.text): return open(m.text, "rb").read()
    return wa.download_media(m.media_id)

def _field_rows(f, lang):
    """Only the fields found, one line each, then what is still missing; a register hit (indexed lookup) is added when there is exactly one."""
    rows = [f"{t('f_manufacturer', lang)}: {f.manufacturer}"] if f.manufacturer else []
    if f.batch:
        rows.append(f"{t('f_batch', lang)}: {f.batch}")
        try: hit = fields.match_register(f.batch)
        except Exception: hit = None                               # DB trouble never blocks the reply
        if hit: rows.append(f"{t('f_register', lang)}: {hit['brand_name']} · {hit['batch_number']}")
    if f.expiry: rows.append(f"{t('f_expiry', lang)}: {f.expiry[1]:02d}/{f.expiry[0]}")
    if f.licence: rows.append(f"{t('f_licence', lang)}: {f.licence}")
    missing = [t(k, lang) for k, v in (("f_batch", f.batch), ("f_expiry", f.expiry)) if not v]
    return rows + ([t("f_missing", lang, names=", ".join(missing))] if missing else [])

def _send_read_text(to, ph, lang, raw):
    """Follow-up message with only the required fields read from the photo (fields.py). Never blocks or changes the verdict; the text itself is not logged or stored."""
    try:
        x = extract.extract_text(raw)
        f = fields.filter_text(x.text)
        log.event("extract", who=ph[:8], ok=x.ok, chars=len(x.text), conf=x.mean_conf, engine=x.engine, langs=x.langs, found=f.any())
        if f.any(): wa.send_text(to, t("read_text", lang, rows="\n".join(_field_rows(f, lang))))
    except Exception as e:
        log.event("extract_failed", who=ph[:8], error=type(e).__name__, level="warn")

def _save(ph, kind, m):
    """Download + validate + EXIF-strip + store an inbound attachment -> upload reference, or None (user told why)."""
    try:
        return storage.save_media(ph, kind, _fetch(m), m.mime)
    except Exception as e:                      # rejected type/size, unreadable image, Graph/network failure
        log.event("media_rejected", who=ph[:8], kind=kind, error=type(e).__name__, reason=str(e)[:100], level="warn"); return None

def _handle(m: wa.Inbound):
    if not _claim(m.wamid): return
    to, ph = m.sender, wa.identity_hash(m.sender)
    s = _session(ph); lang = s["language"] or "en"; state = s["state"]; ctx = s["context"] or {}
    cmd = (m.text or "").strip().upper()
    if s["consented_at"] and state not in ("MENU", "LANG") and (s.get("idle_s") or 0) > IDLE_RESET_S:
        _set(ph, "MENU"); state = "MENU"; log.event("idle_reset", who=ph[:8])   # stale half-finished flow: back to the menu

    # --- consent gate: nothing else is processed until the user agrees ---
    if not s["consented_at"]:
        if state == "CONSENT" and m.kind == "button" and m.text == "agree":
            db.run("UPDATE wa_sessions SET consented_at = now() WHERE phone_hash = %s", (ph,))
            db.run("INSERT INTO consent_log (id, user_identifier_hash, consent_type, granted_at, channel) VALUES (gen_random_uuid(), %s, 'data_processing', now(), 'whatsapp')", (ph,))
            _set(ph, "LANG"); return _lang_buttons(to)
        if state == "CONSENT" and m.kind == "button" and m.text == "decline":
            db.run("DELETE FROM wa_sessions WHERE phone_hash = %s", (ph,)); return wa.send_text(to, t("declined"))
        _set(ph, "CONSENT"); return _consent_prompt(to)

    # --- global commands ---
    if cmd in ("DELETE", "DELETE MY DATA"):
        _set(ph, "DELETE_CONFIRM"); return wa.send_text(to, t("delete_confirm", lang))
    if state == "DELETE_CONFIRM":
        if cmd == "YES":
            log.event("erase", who=ph[:8], **privacy.erase(ph))
            return wa.send_text(to, t("delete_done", lang))
        _set(ph, "MENU"); return wa.send_text(to, t("menu", lang))
    if cmd == "LANG" or state == "LANG" and m.kind != "button":
        _set(ph, "LANG"); return _lang_buttons(to)
    if state == "LANG" and m.kind == "button" and m.text.startswith("lang_"):
        lang = m.text[5:]; _set(ph, "MENU", language=lang); return wa.send_text(to, t("menu", lang))
    if cmd in ("MENU", "HI", "HELLO", "START"):
        _set(ph, "MENU"); return wa.send_text(to, t("menu", lang))
    if cmd == "HELP":
        _set(ph, "MENU"); return wa.send_text(to, t("help", lang))
    if cmd == "TIMELINE" or cmd.startswith("TIMELINE "):
        b = cmd[8:].strip() or ctx.get("last_batch") or ""
        if not BATCH_RE.match(b): return wa.send_text(to, t("timeline_usage", lang))
        return wa.send_text(to, timeline.format_timeline(b, lang))
    if cmd == "WHY": return wa.send_text(to, provenance(ctx, lang))

    # --- multi-step flows ---
    if state == "AWAIT_EXPIRY":
        exp = None if cmd == "SKIP" else _parse_expiry(m.text)
        if cmd != "SKIP" and exp is None: return wa.send_text(to, t("ask_expiry", lang, b=ctx.get("pending_batch", "")))
        return _verify_manual(to, ph, lang, ctx["pending_batch"], exp)
    if state == "REPORT_TYPE" and m.kind == "button" and m.text.startswith("rt"):
        _set(ph, "REPORT_DESC", {"report_type": {"rt1": "suspected_counterfeit", "rt2": "seal_tampering", "rt3": "other"}[m.text]}); return wa.send_text(to, t("report_desc", lang))
    if state == "REPORT_DESC":
        ref = None
        if m.kind in ("image", "document"):
            ref = _save(ph, "report", m)
            if not ref: return wa.send_text(to, t("media_bad", lang))
        elif m.kind != "text": return wa.send_text(to, t("report_desc", lang))
        n = db.one("SELECT count(*) AS n FROM reports WHERE reporter_identity_hash = %s AND created_at > now() - interval '1 day'", (ph,))["n"]
        if n >= config.DAILY_REPORT_CAP: _set(ph, "MENU"); return wa.send_text(to, t("report_cap", lang))
        desc = None if (cmd == "SKIP" or m.kind != "text") else m.text[:500]
        rid = db.one("""INSERT INTO reports (id, scan_id, image_reference, report_type, description, reporter_identity_hash, created_at, status, visibility)
                        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, now(), 'open', 'regulator_only') RETURNING id""",
                     (ctx["last_scan_id"], ref, ctx.get("report_type", "other"), desc, ph))["id"]
        if ref: storage.attach(ref, "report", rid)
        log.event("report_filed", who=ph[:8], with_photo=bool(ref))
        _set(ph, "MENU"); return wa.send_text(to, t("report_ok", lang))
    if state == "DISPUTE_EMAIL":
        dom = m.text.split("@")[-1].strip().lower() if "@" in m.text else ""
        row = db.one("""SELECT md.manufacturer FROM manufacturer_domains md JOIN batches b ON b.manufacturer = md.manufacturer
                        WHERE md.domain = %s AND b.batch_number = %s""", (dom, ctx.get("dispute_batch")))
        if not row: _set(ph, "MENU"); return wa.send_text(to, t("dispute_bad", lang))
        if db.one("SELECT count(*) AS n FROM disputes WHERE submitter_domain = %s AND submitted_at > now() - interval '1 day'", (dom,))["n"] >= DISPUTE_DAILY_CAP:
            _set(ph, "MENU"); return wa.send_text(to, t("dispute_cap", lang))
        _set(ph, "DISPUTE_EVIDENCE", {"dispute_by": row["manufacturer"], "dispute_domain": dom}); return wa.send_text(to, t("dispute_evid", lang))
    if state == "DISPUTE_EVIDENCE":
        up = None
        if m.kind in ("image", "document"):
            up = _save(ph, "dispute", m)
            if not up: return wa.send_text(to, t("media_bad", lang))
        elif m.kind != "text": return wa.send_text(to, t("dispute_evid", lang))
        ref = f"upload:{up}" if up else f"note:{m.text[:200]}"
        b = ctx["dispute_batch"]
        did = db.one("""INSERT INTO disputes (id, batch_number, submitted_by, submitter_domain, evidence_reference, status, submitted_at, submitter_identity_hash, channel)
                        VALUES (gen_random_uuid(), %s, %s, %s, %s, 'open', now(), %s, 'whatsapp') RETURNING id""", (b, ctx["dispute_by"], ctx["dispute_domain"], ref, ph))["id"]
        if up: storage.attach(up, "dispute", did)
        log.event("dispute_filed", who=ph[:8], batch=b, with_media=bool(up))
        db.run("UPDATE batches SET dispute_status = 'open' WHERE batch_number = %s", (b,))
        db.run("INSERT INTO audit_log (id, actor, action, target, timestamp) VALUES (gen_random_uuid(), 'system', 'dispute.submitted', %s, now())", (b,))
        _set(ph, "MENU"); return wa.send_text(to, t("dispute_ok", lang, b=b))

    # --- MENU-level input ---
    if m.kind == "button":
        if m.text == "again": return wa.send_text(to, t("menu", lang))
        if m.text == "report" and ctx.get("last_scan_id"):
            _set(ph, "REPORT_TYPE"); return wa.send_buttons(to, t("report_type", lang), [("rt1", t("btn_rt1", lang)), ("rt2", t("btn_rt2", lang)), ("rt3", t("btn_rt3", lang))])
        if m.text == "dispute" and ctx.get("last_batch"):
            _set(ph, "DISPUTE_EMAIL", {"dispute_batch": ctx["last_batch"]}); return wa.send_text(to, t("dispute_email", lang, b=ctx["last_batch"]))
    if cmd.startswith("DISPUTE"):
        b = cmd.split(" ", 1)[1].strip() if " " in cmd else ctx.get("last_batch")
        if b: _set(ph, "DISPUTE_EMAIL", {"dispute_batch": b}); return wa.send_text(to, t("dispute_email", lang, b=b))
    if cmd == "REPORT" and ctx.get("last_scan_id"):
        _set(ph, "REPORT_TYPE"); return wa.send_buttons(to, t("report_type", lang), [("rt1", t("btn_rt1", lang)), ("rt2", t("btn_rt2", lang)), ("rt3", t("btn_rt3", lang))])
    if m.kind == "image":
        if not _allowed_lookup(ph): return wa.send_text(to, t("wait", lang))
        try:
            raw = _fetch(m)
            v, rec, notes = vision.verify_photo(raw)
        except Exception as e:
            log.event("photo_failed", who=ph[:8], error=type(e).__name__, level="warn"); return wa.send_text(to, t("qr_fail", lang))
        if "QR_FAIL" in notes:
            _send_read_text(to, ph, lang, raw)
            return wa.send_text(to, t("qr_fail", lang))                        # one tap back to manual entry
        deliver_verdict(to, ph, lang, v, rec, {"code_validity", "label_consistency"} if "OCR_LOW_CONF" not in notes else {"code_validity"}, notes)
        return _send_read_text(to, ph, lang, raw)
    if m.kind == "text" and BATCH_RE.match(cmd) and cmd not in KEYWORDS:
        if not _allowed_lookup(ph): return wa.send_text(to, t("wait", lang))
        known = engine.batch_record(cmd) or db.one("SELECT 1 FROM nsq_alerts WHERE batch_number = %s LIMIT 1", (cmd,))
        if not known:
            _set(ph, "AWAIT_EXPIRY", {"pending_batch": cmd}); return wa.send_text(to, t("ask_expiry", lang, b=cmd))
        return _verify_manual(to, ph, lang, cmd, None)
    if m.kind not in ("text", "button"):                   # audio, sticker, location, contacts, video, a stray document, types Meta adds later
        return wa.send_text(to, t("unsupported", lang))
    return wa.send_text(to, t("unknown", lang))

def _verify_manual(to, ph, lang, batch, expiry):
    v, rec = engine.verify_manual(batch, expiry)
    rec = rec or {"batch_number": batch.upper(), "brand_name": None, "product_code": ""}
    deliver_verdict(to, ph, lang, v, rec, set())
