"""Loads REAL CDSCO NSQ rows into nsq_alerts, flagged data_origin='cdsco_real'.
A wrong batch number = a wrongly accused manufacturer, so every row is validated and (PDF mode) page-text guarded.

Mode A -- from the official monthly PDFs (default when no CSV is given):
  python api/scripts/load_real_nsq.py --from-pdfs [--pdf-dir data/real/pdfs] [--confidence medium] [--dry-run]
  Parses PDFs (download them with `python -m ingest.scheduler --once` from api/, or manually into data/real/pdfs/),
  writes data/real/nsq_real_staging.csv (all candidates), nsq_real_rejected.csv (rejects + reasons),
  nsq_real_loaded.csv (accepted, committed), spotcheck.md (15 random accepted rows), loads them and prints a report.
Mode B -- from a hand-checked CSV:
  python api/scripts/load_real_nsq.py real_nsq.csv [--confidence high] [--dry-run]
Other:
  --pick-demo   list DEMO_REAL_BATCH candidates (simple unique batch, not in the seed batches table)

Columns: product_name,batch_number,manufacturer,alert_date(YYYY-MM-DD),reason,source_document,lab_type(central|state),extraction_confidence
--confidence sets the value written for every loaded row (default medium; use 'high' only after a human spot-check of the rows against the source PDFs)."""
import argparse, collections, csv, os, random, re, sys
from datetime import date
API = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, API)
import db
from ingest import parse
from ingest.scheduler import load_rows, record_run
from datetime import datetime, timezone

ROOT = os.path.dirname(API)
REAL = os.path.join(ROOT, "data", "real")
REQ = ["product_name", "batch_number", "manufacturer", "alert_date", "reason", "source_document", "lab_type", "extraction_confidence"]
CONF = ("high", "medium", "low")


def _write_csv(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _print_rejects(rejected, limit=60):
    by = collections.Counter(x for r in rejected for x in r["reject_reasons"])
    print(f"\nREJECTED rows: {len(rejected)}")
    for reason, n in by.most_common():
        print(f"  {n:4d}  {reason}")
    print("  detail (source, page, batch -> reasons):")
    for r in rejected[:limit]:
        print(f"    {r.get('source_document','?')} p{r.get('page','?')} {r.get('batch_number','')!r}: {'; '.join(r['reject_reasons'])}")
    if len(rejected) > limit:
        print(f"    ... {len(rejected) - limit} more in data/real/nsq_real_rejected.csv")


def spotcheck_md(rows, path, n=15, seed=None):
    pick = random.Random(seed).sample(rows, min(n, len(rows)))
    out = ["# Spot-check: real CDSCO NSQ rows", "",
           "Open each PDF (in `data/real/pdfs/`, or from https://cdsco.gov.in/opencms/opencms/en/Notifications/nsq-drugs/) at the page shown and confirm that",
           "batch number, product, manufacturer and reason match the row. Reply with the count OK / count wrong.",
           "Note: `alert month` is the month named in the PDF title (the PDFs carry no per-row date).", "",
           "| # | source PDF | page | product | batch | manufacturer | reason (NSQ result) | lab | mfg / expiry | alert month |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    for i, r in enumerate(pick, 1):
        out.append(f"| {i} | {esc(r['source_document'])} | {r['page']} | {esc(r['product_name'])} | {esc(r['batch_number'])} | {esc(r['manufacturer'])} | "
                   f"{esc(r['reason'])} | {r['lab_type']} ({esc(r.get('reported_by',''))}) | {r.get('mfg_date','')} / {r.get('expiry_date','')} | {str(r['alert_date'])[:7]} |")
    out += ["", "Result: ___ OK / ___ wrong", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def pick_demo(rows):
    """Simple (letters+digits, no separators), unique batch, absent from the seed batch register and synthetic alerts."""
    cnt = collections.Counter(r["batch_number"].upper() for r in rows)
    seed = {x["b"] for x in db.all_("SELECT upper(batch_number) AS b FROM batches UNION SELECT upper(batch_number) FROM nsq_alerts WHERE data_origin='synthetic'")}
    ok = [r for r in rows if cnt[r["batch_number"].upper()] == 1 and r["batch_number"].upper() not in seed
          and re.fullmatch(r"[A-Z]{1,3}\d{4,6}", r["batch_number"].upper()) and len(r["manufacturer"]) < 40
          and re.search(r"\b(ltd|limited|pvt|private)\b", r["manufacturer"], re.I)]
    return ok


def from_pdfs(pdf_dir, conf, dry):
    started = datetime.now(timezone.utc)
    accepted, rejected, docs = parse.parse_dir(pdf_dir)
    if not docs:
        sys.exit(f"no PDFs in {pdf_dir}: run `python -m ingest.scheduler --once` (from api/) or download them manually")
    for r in accepted:
        r["extraction_confidence"] = conf
    bad = parse.verify_rows(accepted, pdf_dir)
    cand = len(accepted) + len(rejected)
    print("documents:")
    for d in docs:
        print(f"  {d['file']}: accepted {d['accepted']}, rejected {d['rejected']}")
    print(f"\ncandidate rows: {cand}   accepted: {len(accepted)}   rejected: {len(rejected)}   guard violations among accepted: {len(bad)}")
    _print_rejects(rejected)
    if bad:
        for r, why in bad:
            print("GUARD VIOLATION:", r["source_document"], r["page"], r["batch_number"], why)
        sys.exit("guard violations: nothing loaded")
    os.makedirs(REAL, exist_ok=True)
    cols = REQ + ["page", "mfg_date", "expiry_date", "reported_by"]
    _write_csv(os.path.join(REAL, "nsq_real_staging.csv"), accepted, cols)
    _write_csv(os.path.join(REAL, "nsq_real_rejected.csv"),
               [{**r, "reject_reasons": "; ".join(r["reject_reasons"])} for r in rejected],
               ["source_document", "page", "product_name", "batch_number", "manufacturer", "mfg_date", "expiry_date", "reject_reasons"])
    if dry:
        print("\n(dry run: nothing loaded, loaded/spot-check files not written)")
        return
    res = load_rows(accepted, conf)
    record_run("load_real_nsq.py", res["inserted"], "ok" if res["inserted"] else "no_new_rows",
               f"{len(docs)} PDFs; {len(accepted)} accepted, {len(rejected)} rejected; {res['existing']} already present", started)
    _write_csv(os.path.join(REAL, "nsq_real_loaded.csv"), accepted, cols)
    spotcheck_md(accepted, os.path.join(REAL, "spotcheck.md"), seed=20250719)
    print(f"\nloaded: inserted {res['inserted']}, already present {res['existing']}, confidence updated {res['confidence_updated']} "
          f"(data_origin=cdsco_real, extraction_confidence={conf})")
    print("wrote data/real/nsq_real_loaded.csv, nsq_real_staging.csv, nsq_real_rejected.csv, spotcheck.md")
    demo = pick_demo(accepted)
    print("DEMO_REAL_BATCH candidates:", [(r["batch_number"], r["product_name"][:30], r["manufacturer"]) for r in demo[:5]] or "none")


def from_csv(path, conf, dry):
    rows, rejected = [], []
    for i, r in enumerate(csv.DictReader(open(path, encoding="utf-8-sig")), start=2):
        if not any(r.values()): continue
        why = [f"missing {k}" for k in REQ if k != "extraction_confidence" and not (r.get(k) or "").strip()]
        if r.get("lab_type") not in ("central", "state"): why.append("lab_type must be central|state")
        try: date.fromisoformat(r.get("alert_date", ""))
        except ValueError: why.append("alert_date must be YYYY-MM-DD")
        if why: rejected.append({**r, "page": f"line {i}", "reject_reasons": why}); continue
        rows.append({**r, "batch_number": r["batch_number"].strip().upper(), "alert_date": r["alert_date"], "extraction_confidence": conf})
    clash = [r["batch_number"] for r in rows if db.one("SELECT 1 FROM nsq_alerts WHERE batch_number=%s AND data_origin='synthetic'", (r["batch_number"],))]
    if clash: print("WARNING: these batch numbers also exist in the SYNTHETIC set; the verdict may mix demo and real rows:", clash)
    print(f"{len(rows)} valid rows, {len(rejected)} rejected" + (" (dry run, nothing written)" if dry else ""))
    if rejected: _print_rejects(rejected)
    if dry or not rows: return
    res = load_rows(rows, conf)
    print(f"loaded: inserted {res['inserted']}, already present {res['existing']}, confidence updated {res['confidence_updated']}")
    print("Verify one by hand:  python -c \"import engine;print(engine.verify_manual('<BATCH>')[0].verdict)\"")


def main():
    ap = argparse.ArgumentParser(description="Load real CDSCO NSQ rows", usage=__doc__)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--from-pdfs", action="store_true")
    ap.add_argument("--pdf-dir", default=os.path.join(REAL, "pdfs"))
    ap.add_argument("--confidence", choices=CONF, default="medium")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pick-demo", action="store_true")
    a = ap.parse_args()
    if a.confidence == "high":
        print("NOTE: 'high' is only justified after a human spot-check of the rows against the source PDFs.")
    if a.pick_demo:
        rows = list(csv.DictReader(open(os.path.join(REAL, "nsq_real_loaded.csv"), encoding="utf-8")))
        for r in pick_demo(rows)[:10]:
            print(r["batch_number"], "|", r["product_name"], "|", r["manufacturer"], "|", r["source_document"], "p", r["page"])
    elif a.csv:
        from_csv(a.csv, a.confidence, a.dry_run)
    elif a.from_pdfs:
        from_pdfs(a.pdf_dir, a.confidence, a.dry_run)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
