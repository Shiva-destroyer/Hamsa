"""CDSCO monthly NSQ PDF parser + page-text guard + validator + dedupe.

Pipeline:  PDF --pdfplumber tables--> raw rows --check_row (validator + guard)--> accepted / rejected --dedupe--> staging.
Nothing is ever invented: every field comes from a table cell of the PDF; a row that cannot be confirmed against the page
text is rejected with a reason, never "fixed".
"""
import calendar
import os
import re
import unicodedata
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

OUT_COLUMNS = ["product_name", "batch_number", "manufacturer", "alert_date", "reason", "source_document", "lab_type",
               "extraction_confidence", "page", "mfg_date", "expiry_date", "reported_by"]

# Guard windows (tokens) around the batch position in the page text. Cells wrap, so text of one row is interleaved
# with neighbouring columns; product precedes the batch in reading order, manufacturer follows it.
_WIN_BEFORE, _WIN_AFTER = 60, 160


# ---- text helpers ---------------------------------------------------------------------------
def tokens(s: str) -> list[str]:
    """Lower-case alphanumeric runs; punctuation and line-break hyphens are ignored ('Bio-\\nsciences' == 'Bio- sciences')."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.findall(r"[^\W_]+", s)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def _subseq_in(needle: list[str], hay: list[str]) -> bool:
    it = iter(hay)
    return bool(needle) and all(any(t == h for h in it) for t in needle)


def _find_contig(needle: list[str], hay: list[str]) -> list[int]:
    n = len(needle)
    return [i for i in range(len(hay) - n + 1) if hay[i:i + n] == needle] if n else []


# ---- date parsing ---------------------------------------------------------------------------
def parse_month_year(s: str):
    """'08/2024', '8/24', 'Apr-24', 'APR 2024' -> date(y, m, 1); None if it does not parse."""
    s = re.sub(r"\s+", "", s).replace(".", "")          # PDF char-spacing artefacts ('F e b - 2 5') collapse here
    s = re.sub(r"^\d{1,2}[/\-](\d{1,2}[/\-]\d{2,4})$", r"\1", s)     # dd-mm-yyyy -> mm-yyyy
    m = re.fullmatch(r"(\d{1,2})[/\-](\d{2}|\d{4})", s)
    if m:
        mo, yr = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"([A-Za-z]{3,9})[-/,]*(\d{2}|\d{4})", s)
        if not m or m.group(1)[:3].lower() not in MONTHS:
            return None
        mo, yr = MONTHS[m.group(1)[:3].lower()], int(m.group(2))
    if yr < 100:
        yr += 2000
    if not (1 <= mo <= 12 and 1990 <= yr <= 2100):
        return None
    return date(yr, mo, 1)


def month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


# ---- document-level facts -------------------------------------------------------------------
def doc_is_nsq(page1_text: str) -> bool:
    return "not of standard quality" in re.sub(r"\s+", " ", page1_text[:600].lower())


def detect_alert_month(page1_text: str, filename: str = ""):
    """(year, month) from 'ALERT FOR THE MONTH OF JUNE-2025'; falls back to the filename; else None."""
    m = re.search(r"MONTH\s+OF\s+([A-Za-z]+)\W{0,4}\s*(20\d\d)", page1_text[:900], re.I)
    if m and m.group(1)[:3].lower() in MONTHS:
        return int(m.group(2)), MONTHS[m.group(1)[:3].lower()]
    return None


def detect_lab_type(page1_text: str, filename: str = ""):
    """'central' | 'state' | None. Document section heading first, filename second."""
    head = page1_text[:600]
    if re.search(r"state\s+laborator", head, re.I):
        return "state"
    if re.search(r"central\s+laborator|CDSCO/", head, re.I):
        return "central"
    f = os.path.basename(filename).lower()
    if re.search(r"state|stnsq", f):
        return "state"
    if re.search(r"cdsco|central", f):
        return "central"
    return None


# ---- manufacturer name (cell holds name + address) -----------------------------------------
_SUFFIX = re.compile(r"\b(?:Pvt\.?,?\s*Ltd\.?|Private\s+Limited|P\)?\s*Ltd\.?|Limited|Ltd\.?|LLP)(?![A-Za-z])", re.I)


_ADDRESS = re.compile(r"\s(?:Plot|Vill\w*|Sector|Survey|Khasra|Block|Unit|Near|Behind|Opp\w*|Gala|Shed|Mauza|Kh\.?)\b|\s\d", re.I)


def manufacturer_name(cell: str) -> str:
    """Company name from 'M/s. Name Pvt. Ltd., address...': the shorter of (text up to the first comma) and (text up to the
    first company suffix Ltd/Limited/LLP). Always a prefix of the printed cell, so it can be guarded against the page text."""
    c = clean(cell)
    c = re.sub(r"^M\s*/\s*s\.?\s*", "", c, flags=re.I)
    parts = c.split(",")
    name, i = parts[0].strip(), 1
    while i < len(parts) and re.search(r"\b(pvt|private|p)\.?$", name, re.I) and re.match(r"\s*(ltd|limited)\b", parts[i], re.I):
        name = f"{name}, {parts[i].strip()}"
        i += 1
    m = _SUFFIX.search(c)
    if m:
        cut = c[:m.end()].strip().rstrip(",")
        if len(cut) < len(name):
            name = cut
    a = _ADDRESS.search(name)
    if a and a.start() >= 3:
        name = name[:a.start()].strip(" ,-")
    return name if len(name) >= 3 else c


# ---- validator + page-text guard ------------------------------------------------------------
def guard(row: dict, page_text: str) -> list[str]:
    """Page-text guard. Returns reasons (empty = pass): the batch string, product name and manufacturer stored on the row must
    each appear in the source page text (alphanumeric tokens, in order; product/manufacturer must sit near the batch)."""
    page = tokens(page_text)
    bt = tokens(row.get("batch_number", ""))
    starts = _find_contig(bt, page)
    if not starts and bt:                                   # wrapped batch: pieces must follow each other closely
        starts = [i for i in range(len(page)) if page[i] == bt[0] and _subseq_in(bt, page[i:i + len(bt) + 40])]
    if not starts:
        return ["guard: batch not found in page text"]
    reasons = []
    for label, need in (("product", tokens(row.get("product_name", ""))), ("manufacturer", tokens(row.get("manufacturer", "")))):
        if not any(_subseq_in(need, page[max(0, s - _WIN_BEFORE):s + _WIN_AFTER]) for s in starts):
            reasons.append(f"guard: {label} not found in page text near batch")
    return reasons


def check_row(row: dict, page_text: str) -> list[str]:
    """All reasons a candidate row must be rejected (empty list = accept). Order: field presence, formats, dates, guard."""
    why = []
    batch = clean(row.get("batch_number", ""))
    if not clean(row.get("product_name", "")):
        why.append("missing product name")
    if not clean(row.get("manufacturer", "")):
        why.append("missing manufacturer")
    if not batch:
        why.append("missing batch number")
    elif re.fullmatch(r"(?i)(not\s*mentioned|not\s*available|not\s*stated|nm|nil|na|n/?a|none|-+)\.?", batch.replace(" ", "")) or \
            re.fullmatch(r"(?i)not\s*(mentioned|available|stated)", batch):
        why.append("batch number not stated in document")
    elif not re.search(r"\d", batch) and len(batch) < 4:
        why.append("batch number implausible")
    elif len(batch) > 30 or re.search(r"[,;]|\s(?:and|&)\s", batch, re.I):
        why.append("batch cell holds multiple/unparseable batches")
    for f in ("product_name", "manufacturer", "batch_number"):
        if re.search(r"(?:(?<=\s)|^)\w(?:\s\w){3,}(?=\s|$)", clean(row.get(f, ""))):
            why.append(f"{f}: character-spaced text extraction artefact")
    if re.search(r"\b(?:mkt|mkd|mft|mfd|mfg|marketed|manufactured)\.?\s*by\b|^(?:by|for)\b", clean(row.get("manufacturer", "")), re.I):
        why.append("manufacturer cell names several parties (manufacturer/marketer); not attributed")
    if not clean(row.get("reason", "")):
        why.append("missing reason")
    if row.get("lab_type") not in ("central", "state"):
        why.append("lab_type undeterminable from document section/filename")
    ad = row.get("alert_date")
    if not isinstance(ad, date):
        try:
            ad = date.fromisoformat(str(ad))
        except ValueError:
            ad = None
    if ad is None:
        why.append("alert date does not parse")
    md, ed = parse_month_year(row.get("mfg_date", "")), parse_month_year(row.get("expiry_date", ""))
    if md is None:
        why.append(f"manufacturing date does not parse ({clean(row.get('mfg_date', ''))!r})")
    if ed is None:
        why.append(f"expiry date does not parse ({clean(row.get('expiry_date', ''))!r})")
    if md and ed and ed < md:
        why.append("expiry date precedes manufacturing date")
    return why + guard(row, page_text)


def dedupe_key(row: dict) -> tuple:
    return (clean(row["batch_number"]).upper(), " ".join(tokens(row["manufacturer"])), str(row["alert_date"]))


def dedupe(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep the first row per (batch, manufacturer, alert_date). Returns (kept, duplicates)."""
    seen, kept, dups = set(), [], []
    for r in rows:
        k = dedupe_key(r)
        (dups if k in seen else kept).append(r)
        seen.add(k)
    return kept, dups


# ---- PDF extraction -------------------------------------------------------------------------
def _column_map(header: list[str]):
    """Map logical columns to indexes using the (whitespace-stripped) header text; None if the layout is not recognised."""
    m = {}
    for i, c in enumerate(header):
        h = re.sub(r"\s+", "", (c or "")).lower()
        if "batch" in h: m["batch"] = i
        elif "manufacturedby" in h: m["mfr"] = i
        elif "nsqresult" in h or "reason" in h: m["reason"] = i
        elif "reported" in h: m["lab"] = i
        elif "expiry" in h: m["expiry"] = i
        elif h.startswith("manufact") and "date" in h: m["mfg"] = i
        elif "product" in h or "drug" in h or h == "name": m["product"] = i
    return m if {"product", "batch", "mfr", "reason", "mfg", "expiry"} <= set(m) else None


def extract_pdf(path: str):
    """Returns (raw_rows, doc_info, page_texts). raw_rows: dicts with the untouched cell strings + pages spanned."""
    import pdfplumber
    fname = os.path.basename(path)
    raws, cmap, ncols = [], None, None
    with pdfplumber.open(path) as pdf:
        page_texts = [p.extract_text() or "" for p in pdf.pages]
        info = {"filename": fname, "nsq": doc_is_nsq(page_texts[0]) if page_texts else False,
                "alert_month": detect_alert_month(page_texts[0], fname) if page_texts else None,
                "lab_type": detect_lab_type(page_texts[0], fname) if page_texts else None, "pages": len(page_texts)}
        if not info["nsq"]:
            return [], info, page_texts
        for pno, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables():
                for cells in table:
                    cells = [c or "" for c in cells]
                    if len(cells) < 6:
                        continue
                    if re.match(r"s\.?\s*no", clean(cells[0]), re.I):           # header row
                        cm = _column_map(cells)
                        if cm:
                            cmap, ncols = cm, len(cells)
                        continue
                    if cmap is None or len(cells) != ncols:
                        continue
                    if re.fullmatch(r"\d+\.?", clean(cells[0])):                 # new numbered row
                        raws.append({"sno": clean(cells[0]), "cells": {k: cells[i] for k, i in cmap.items()}, "pages": [pno]})
                    elif raws and not clean(cells[0]) and not clean(cells[cmap["batch"]]):   # continuation of a wrapped row
                        r = raws[-1]
                        for k, i in cmap.items():
                            if clean(cells[i]):
                                r["cells"][k] = (r["cells"].get(k, "") + "\n" + cells[i]).strip("\n")
                        if pno not in r["pages"]:
                            r["pages"].append(pno)
                    elif clean(cells[0]) or clean(cells[cmap["batch"]]):
                        raws.append({"sno": clean(cells[0]), "cells": {k: cells[i] for k, i in cmap.items()}, "pages": [pno], "unnumbered": True})
    return raws, info, page_texts


def _join_hyphen_breaks(s: str) -> str:
    return clean(re.sub(r"-\s*\n\s*", "-", s))


def build_row(raw: dict, info: dict) -> dict:
    c = raw["cells"]
    am = info.get("alert_month")
    return {
        "product_name": clean(c.get("product", "")),
        "batch_number": re.sub(r"\s+", "", _join_hyphen_breaks(c.get("batch", ""))) if "\n" in c.get("batch", "").strip() else clean(c.get("batch", "")),
        "manufacturer": manufacturer_name(_join_hyphen_breaks(c.get("mfr", ""))),
        "alert_date": month_end(*am) if am else None,
        "reason": clean(c.get("reason", "")),
        "source_document": info["filename"],
        "lab_type": info.get("lab_type"),
        "extraction_confidence": "medium",
        "page": raw["pages"][0] if len(raw["pages"]) == 1 else f"{raw['pages'][0]}-{raw['pages'][-1]}",
        "mfg_date": clean(c.get("mfg", "")),
        "expiry_date": clean(c.get("expiry", "")),
        "reported_by": clean(c.get("lab", "")),
        "_pages": raw["pages"], "_sno": raw["sno"],
    }


def parse_pdf(path: str):
    """Returns (accepted, rejected). Each rejected row carries `reject_reasons`. Duplicates are rejected with reason 'duplicate'."""
    raws, info, page_texts = extract_pdf(path)
    if not info["nsq"]:
        return [], [{"source_document": info["filename"], "page": "", "product_name": "", "batch_number": "", "manufacturer": "",
                     "reject_reasons": ["document is not an NSQ alert list (skipped)"]}]
    cands, rejected = [], []
    for raw in raws:
        row = build_row(raw, info)
        text = "\n".join(page_texts[p - 1] for p in row["_pages"])
        why = check_row(row, text)
        if raw.get("unnumbered"):
            why.append("row has no serial number (layout not recognised)")
        (rejected if why else cands).append({**row, "reject_reasons": why} if why else row)
    return cands, rejected


def parse_dir(pdf_dir: str):
    """Parse every PDF in pdf_dir. Returns (accepted, rejected, docs). Dedupe across all documents."""
    accepted, rejected, docs = [], [], []
    for fn in sorted(os.listdir(pdf_dir)):
        if not fn.lower().endswith(".pdf"):
            continue
        a, r = parse_pdf(os.path.join(pdf_dir, fn))
        docs.append({"file": fn, "accepted": len(a), "rejected": len(r)})
        accepted += a
        rejected += r
    kept, dups = dedupe(accepted)
    rejected += [{**d, "reject_reasons": ["duplicate of (batch, manufacturer, alert_date) already accepted"]} for d in dups]
    return kept, rejected, docs


def verify_rows(rows: list[dict], pdf_dir: str) -> list[tuple]:
    """Independent re-check of already-accepted rows: re-read the page text of each row's source PDF and re-run the guard.
    Returns [(row, reasons)] for every guard violation (should be empty)."""
    import pdfplumber
    cache, bad = {}, []
    for r in rows:
        fn = r["source_document"]
        if fn not in cache:
            with pdfplumber.open(os.path.join(pdf_dir, fn)) as pdf:
                cache[fn] = [p.extract_text() or "" for p in pdf.pages]
        pages = [int(x) for x in str(r["page"]).split("-")]
        pages = range(pages[0], pages[-1] + 1)
        why = guard(r, "\n".join(cache[fn][p - 1] for p in pages))
        if why:
            bad.append((r, why))
    return bad
