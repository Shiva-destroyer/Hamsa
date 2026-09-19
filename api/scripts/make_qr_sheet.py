"""One A4 page with 15 mixed sample packs (QR + printed label) that together cover every demo verdict.
Run AFTER seed + demo_fixups.sql:   python scripts/make_qr_sheet.py [--export] [--verify]   -> assets/packs/QR_SHEET_15.pdf (+ assets/packs/cells/NN_BATCH.png with --export)
Print at 100% on plain white paper, cut off the presenter key, photograph ONE cell at a time (the paper, never a screen).
--verify renders the PDF back to pixels (pdftoppm) and checks every cell gives its expected outcome."""
import os, subprocess, sys, tempfile, zlib
from PIL import Image, ImageDraw, ImageFilter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_demo_packs import _symbol, font
import db

PAGE_W, PAGE_H = 2480, 3508                        # A4 at 300 dpi
COLS, ROWS, MARGIN, TOP = 3, 5, 50, 130
CELL_W, CELL_H = (PAGE_W - 2 * MARGIN) // COLS, 580
KEY_Y = TOP + ROWS * CELL_H + 40

# Mixed on purpose (no two neighbours share a verdict). expect = (verdict or None, note that must appear or None); None verdict = bot asks for a typed batch.
CASES = [
    dict(batch="MQ7756", expect=("GREEN", None), key="clean pack -> GREEN"),
    dict(batch="AX2291", expect=("RED", None), key="official NSQ match -> RED"),
    dict(batch="GH6625", printed="Zenith Labs Pvt. Ltd.", expect=("AMBER", None), key="printed manufacturer differs -> AMBER"),
    dict(batch="EN3302", expect=("EXPIRED", None), key="expired -> EXPIRED"),
    dict(batch="CT5510", printed="Alpha Remedies Ltd.", expect=("RED", None), key="differs + replay flag -> RED escalated"),
    dict(batch="NS3340", expect=("GREEN", None), key="clean pack -> GREEN"),
    dict(batch="AX2291", payload="HTTP://FAKE-VERIFY.EXAMPLE/AX2291", expect=(None, "QR_FAIL"), key="not a GS1 code -> type the batch number"),
    dict(batch="PT8813", expect=("RED", None), key="NSQ + open dispute -> RED, 'under review'"),
    dict(batch="LP2098", blur=3.0, expect=("AMBER", "BLUR"), key="blurred print -> AMBER (visual only)"),
    dict(batch="TX6690", expect=("RED", None), key="dispute upheld -> RED"),
    dict(batch="RV5567", expect=("GREEN", None), key="earlier NSQ listing corrected -> GREEN"),
    dict(batch="BQ7743", printed="Kestrel Lifesciences Pvt. Ltd.", expect=("RED", None), key="differs + replay flag -> RED escalated"),
    dict(batch="JK4471", expect=("AMBER", None), key="replay preview only -> AMBER"),
    dict(batch="FM9184", expect=("RED", None), key="NSQ + expired -> RED, also expired"),
    dict(batch="SW1123", printed_batch="SW1132", expect=("AMBER", None), key="printed batch differs from code -> AMBER"),
]


def cell(n, spec):
    r = db.one("""SELECT b.expiry_date, p.product_code, p.brand_name, p.generic_name, p.manufacturer
                  FROM batches b JOIN products p USING (product_code) WHERE b.batch_number = %s""", (spec["batch"],))
    serial = zlib.crc32(spec["batch"].encode()) % 10**8
    payload = spec.get("payload") or f"(01){r['product_code'].zfill(14)}(10){spec['batch']}(17){r['expiry_date']:%y%m%d}(21)SN{serial:08d}"
    im = Image.new("RGB", (CELL_W, CELL_H), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([4, 4, CELL_W - 5, CELL_H - 5], outline="black", width=4)
    d.text((24, 14), r["brand_name"], fill="black", font=font(44))
    d.text((24, 68), r["generic_name"], fill="black", font=font(26))
    d.text((24, 112), "Mfd. by: " + (spec.get("printed") or r["manufacturer"]), fill="black", font=font(32))
    d.text((24, 158), f"Batch No.: {spec.get('printed_batch') or spec['batch']}", fill="black", font=font(38))
    d.text((24, 208), f"Exp.: {r['expiry_date']:%m/%Y}", fill="black", font=font(38))
    im.paste(_symbol(payload, "qr").resize((300, 300), Image.LANCZOS), (24, 262))
    d.text((360, 290), f"#{n:02d}", fill="black", font=font(96))
    for i, line in enumerate(("SAMPLE PACK", "HAMSA DEMO", "NOT A REAL", "MEDICINE")):
        d.text((360, 410 + i * 32), line, fill="black", font=font(26))
    return im.filter(ImageFilter.GaussianBlur(spec["blur"])) if spec.get("blur") else im


def build(out):
    page = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    d = ImageDraw.Draw(page)
    d.text((MARGIN, 40), "HAMSA - 15 sample packs (mixed) - photograph ONE cell at a time, paper only", fill="black", font=font(40))
    for i, spec in enumerate(CASES):
        page.paste(cell(i + 1, spec), (MARGIN + (i % COLS) * CELL_W, TOP + (i // COLS) * CELL_H))
    d.line([(MARGIN, KEY_Y), (PAGE_W - MARGIN, KEY_Y)], fill="black", width=3)
    d.text((MARGIN, KEY_Y + 14), "PRESENTER KEY - cut off before the demo", fill="black", font=font(30))
    for i, spec in enumerate(CASES):
        d.text((MARGIN + (i // 5) * (PAGE_W - 2 * MARGIN) // 3, KEY_Y + 70 + (i % 5) * 44), f"#{i + 1:02d} {spec['batch']}: {spec['key']}", fill="black", font=font(26))
    page.save(out, "PDF", resolution=300.0, quality=95)
    return out


def cells(pdf):
    """Render the PDF back to pixels and yield (n, spec, PNG bytes) per cell, cropped as a phone would frame it (2x)."""
    import io
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", "300", "-png", "-singlefile", pdf, os.path.join(tmp, "p")], check=True)
        page = Image.open(os.path.join(tmp, "p.png")).convert("RGB")
    assert page.size == (PAGE_W, PAGE_H), page.size
    for i, spec in enumerate(CASES):
        x, y = MARGIN + (i % COLS) * CELL_W, TOP + (i // COLS) * CELL_H
        c = page.crop((x, y, x + CELL_W, y + CELL_H))
        b = io.BytesIO(); c.resize((c.width * 2, c.height * 2), Image.LANCZOS).save(b, "PNG")
        yield i + 1, spec, b.getvalue()


def export(pdf, out_dir):
    """One PNG per cell for manual testing (upload/send them like a photo of the pack) -> {n: path}."""
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for n, spec, png in cells(pdf):
        paths[n] = os.path.join(out_dir, f"{n:02d}_{spec['batch']}.png")
        open(paths[n], "wb").write(png)
    return paths


def verify(pdf):
    """Check every cell (as rendered from the PDF) gives its expected outcome. Returns the list of failing cell numbers."""
    import vision
    bad = []
    for n, spec, png in cells(pdf):
        v, _rec, notes = vision.verify_photo(png)
        want_v, want_note = spec["expect"]
        ok = (want_v is None or v.verdict == want_v) and (want_note is None or want_note in notes)
        if spec["batch"] in ("CT5510", "BQ7743"): ok = ok and v.reason == "escalated_conflict"
        print(f"{'OK  ' if ok else 'FAIL'} #{n:02d} {spec['batch']:7} -> {v.verdict:8} {v.reason or '':20} notes={notes}")
        if not ok: bad.append(n)
    return bad


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "packs", "QR_SHEET_15.pdf")
    print(build(out), "(1 page, 15 packs)")
    if "--export" in sys.argv:
        print(*export(out, os.path.join(os.path.dirname(out), "cells")).values(), sep="\n")
    if "--verify" in sys.argv:
        sys.exit(1 if verify(out) else 0)
