"""Batch timeline. WhatsApp text (en/hi/kn, English fallback) and JSON.

Order: manufactured -> NSQ alerts (with status history incl. corrected/superseded) -> aggregated scan activity by
district (synthetic) -> reports (NEVER counts, never public: one fixed line, shown whether or not reports exist)
-> disputes filed/resolved. Each section is chronological. Neither output ever contains report data.
"""
import db

# hi/kn are AI-drafted pending native review. Any missing key/language falls back to English.
STR = {
    "title": {"en": "Timeline — batch {b}", "hi": "टाइमलाइन — बैच {b}", "kn": "ಟೈಮ್‌ಲೈನ್ — ಬ್ಯಾಚ್ {b}"},
    "manufactured": {"en": "Manufactured by {m} — expiry {e}", "hi": "निर्माता: {m} — समाप्ति {e}", "kn": "ತಯಾರಕ: {m} — ಅವಧಿ ಮುಕ್ತಾಯ {e}"},
    "nsq_head": {"en": "Official CDSCO records", "hi": "आधिकारिक CDSCO रिकॉर्ड", "kn": "ಅಧಿಕೃತ CDSCO ದಾಖಲೆಗಳು"},
    "nsq_row": {"en": "NSQ alert ({r}) — status: {s}. Source: {d}", "hi": "NSQ अलर्ट ({r}) — स्थिति: {s}। स्रोत: {d}",
                "kn": "NSQ ಎಚ್ಚರಿಕೆ ({r}) — ಸ್ಥಿತಿ: {s}. ಮೂಲ: {d}"},
    "st_active": {"en": "ACTIVE", "hi": "सक्रिय", "kn": "ಸಕ್ರಿಯ"},
    "st_corrected": {"en": "CORRECTED — no longer an active listing", "hi": "सुधारा गया — अब सक्रिय सूची नहीं"},
    "st_superseded": {"en": "SUPERSEDED by a later notice", "hi": "बाद की सूचना से बदला गया"},
    "scan_head": {"en": "Scan activity by district (🧪 synthetic demo data)", "hi": "ज़िले के अनुसार स्कैन गतिविधि (🧪 कृत्रिम डेमो डेटा)",
                  "kn": "ಜಿಲ್ಲೆವಾರು ಸ್ಕ್ಯಾನ್ ಚಟುವಟಿಕೆ (🧪 ಕೃತಕ ಡೆಮೊ ಡೇಟಾ)"},
    "scan_row": {"en": "{n} scans, {a} to {z}", "hi": "{n} स्कैन, {a} से {z}", "kn": "{n} ಸ್ಕ್ಯಾನ್, {a} ರಿಂದ {z}"},
    "scan_more": {"en": "…and {n} more districts", "hi": "…और {n} ज़िले"},
    "reports_head": {"en": "Community reports", "hi": "सामुदायिक रिपोर्ट", "kn": "ಸಮುದಾಯ ವರದಿಗಳು"},
    "reports_line": {"en": "Reviewed by the regulator only. Never shown publicly, never counted here.",
                     "hi": "केवल नियामक द्वारा देखी जाती हैं। सार्वजनिक रूप से नहीं दिखाई जातीं, यहाँ गिनती नहीं।",
                     "kn": "ನಿಯಂತ್ರಕರು ಮಾತ್ರ ನೋಡುತ್ತಾರೆ. ಸಾರ್ವಜನಿಕವಾಗಿ ತೋರಿಸುವುದಿಲ್ಲ."},
    "disp_head": {"en": "Disputes (makers)", "hi": "आपत्तियाँ (निर्माता)", "kn": "ಆಕ್ಷೇಪಗಳು (ತಯಾರಕ)"},
    "disp_filed": {"en": "Dispute filed by {m}", "hi": "{m} द्वारा आपत्ति दर्ज", "kn": "{m} ಅವರಿಂದ ಆಕ್ಷೇಪ ಸಲ್ಲಿಕೆ"},
    "disp_upheld": {"en": "Dispute resolved — verdict UPHELD", "hi": "आपत्ति निपटी — निर्णय बरकरार"},
    "disp_overturned": {"en": "Dispute resolved — verdict OVERTURNED", "hi": "आपत्ति निपटी — निर्णय पलटा गया"},
    "disp_open": {"en": "Dispute still under review — the verdict is unchanged meanwhile.", "hi": "आपत्ति की समीक्षा जारी है — तब तक निर्णय वही है।"},
    "none_found": {"en": "No records found for batch {b}.", "hi": "बैच {b} के लिए कोई रिकॉर्ड नहीं मिला।", "kn": "ಬ್ಯಾಚ್ {b} ಗೆ ಯಾವುದೇ ದಾಖಲೆ ಸಿಗಲಿಲ್ಲ."},
    "footer": {"en": "🧪 = synthetic demo record. Replay/scan data is a preview. GREEN never means genuine.",
               "hi": "🧪 = कृत्रिम डेमो रिकॉर्ड। GREEN का मतलब असली नहीं।"},
}
NOTICE_EN = "Scan activity and records tagged as synthetic are demo data for illustration only."
_SCAN_LIMIT = 5


def _t(lang: str, key: str, **kw) -> str:
    d = STR[key]
    return (d.get(lang) or d["en"]).format(**kw)



def _collect(batch: str) -> dict:
    batch = (batch or "").strip().upper()
    b = db.one("""SELECT b.manufacturing_date, b.expiry_date, b.manufacturer, p.brand_name
                    FROM batches b JOIN products p USING (product_code) WHERE b.batch_number = %s""", (batch,))
    events = []
    if b:
        events.append({"kind": "manufactured", "date": b["manufacturing_date"].isoformat(), "manufacturer": b["manufacturer"],
                       "product": b["brand_name"], "expiry": b["expiry_date"].isoformat(), "synthetic": True})
    for r in db.all_("SELECT * FROM nsq_alerts WHERE batch_number = %s ORDER BY alert_date, source_document", (batch,)):
        events.append({"kind": "nsq_alert", "date": r["alert_date"].isoformat(), "status": r["status"], "reason": r["reason"],
                       "source_document": r["source_document"], "synthetic": r["data_origin"] == "synthetic"})
    rows = db.all_(r"""SELECT substring(approx_location from '\(([^,)]+),') AS district, count(*) AS n,
                              min(scanned_at) AS first, max(scanned_at) AS last
                         FROM scan_events WHERE batch_number = %s AND approx_location ~ '\([^,)]+,[^)]+\)'
                        GROUP BY 1 ORDER BY n DESC, 1""", (batch,))
    if rows:
        events.append({"kind": "scan_activity", "date": min(r["first"] for r in rows).date().isoformat(),
                       "last_date": max(r["last"] for r in rows).date().isoformat(), "synthetic": True,
                       "total": sum(r["n"] for r in rows),
                       "districts": [{"district": r["district"].strip(), "scans": r["n"]} for r in rows]})
    for r in db.all_("SELECT * FROM disputes WHERE batch_number = %s ORDER BY submitted_at", (batch,)):
        events.append({"kind": "dispute_filed", "date": r["submitted_at"].date().isoformat(), "by": r["submitted_by"], "status": r["status"]})
        if r["status"] in ("resolved_upheld", "resolved_overturned"):
            events.append({"kind": "dispute_resolved", "date": (r["resolved_at"] or r["submitted_at"]).date().isoformat(),
                           "outcome": r["status"].removeprefix("resolved_"), "notes": (r["resolution_notes"] or "")[:300]})
    return {"batch": batch, "found": bool(events), "product": b["brand_name"] if b else None, "events": events}


def timeline_json(batch: str) -> dict:
    """Same content as format_timeline. Contains no report data of any kind."""
    t = _collect(batch)
    t["synthetic_notice"] = NOTICE_EN
    return t


def format_timeline(batch: str, lang: str = "en") -> str:
    t = _collect(batch)
    lang = lang if lang in ("en", "hi", "kn") else "en"
    if not t["found"]:
        return _t(lang, "none_found", b=t["batch"])
    ev = t["events"]
    head = f"*{_t(lang, 'title', b=t['batch'])}*" + (f" ({t['product']})" if t["product"] else "")
    out = [head, ""]
    for e in (e for e in ev if e["kind"] == "manufactured"):
        out.append(f"📅 {_d_iso(e['date'])} — {_t(lang, 'manufactured', m=e['manufacturer'], e=_d_iso(e['expiry']))} 🧪")
    nsq = [e for e in ev if e["kind"] == "nsq_alert"]
    if nsq:
        out += ["", f"*{_t(lang, 'nsq_head')}*"]
        for e in nsq:
            s = _t(lang, "st_" + e["status"])
            out.append(f"• {_d_iso(e['date'])} — {_t(lang, 'nsq_row', r=e['reason'], s=s, d=e['source_document'])}" + (" 🧪" if e["synthetic"] else ""))
    for e in (e for e in ev if e["kind"] == "scan_activity"):
        out += ["", f"*{_t(lang, 'scan_head')}*"]
        for d in e["districts"][:_SCAN_LIMIT]:
            out.append(f"• {d['district']}: {d['scans']}")
        if len(e["districts"]) > _SCAN_LIMIT:
            out.append(_t(lang, "scan_more", n=len(e["districts"]) - _SCAN_LIMIT))
        out.append(_t(lang, "scan_row", n=e["total"], a=_d_iso(e["date"]), z=_d_iso(e["last_date"])))
    out += ["", f"*{_t(lang, 'reports_head')}*", _t(lang, "reports_line")]
    disp = [e for e in ev if e["kind"] in ("dispute_filed", "dispute_resolved")]
    if disp:
        out += ["", f"*{_t(lang, 'disp_head')}*"]
        for e in disp:
            if e["kind"] == "dispute_filed":
                out.append(f"• {_d_iso(e['date'])} — {_t(lang, 'disp_filed', m=e['by'])}")
                if e["status"] in ("open", "under_review"):
                    out.append(f"  ↳ {_t(lang, 'disp_open')}")
            else:
                out.append(f"• {_d_iso(e['date'])} — {_t(lang, 'disp_' + e['outcome'])}" + (f"\n  ↳ {e['notes']}" if e["notes"] else ""))
    out += ["", f"_{_t(lang, 'footer')}_"]
    return "\n".join(out)


def _d_iso(s: str) -> str:
    y, m, d = s.split("-")
    return f"{d}/{m}/{y}"
