"""Generates printable 'sample pack' PNGs (QR + printed label) for the WhatsApp demo photo path.
Run AFTER seed + demo_fixups.sql:   python scripts/make_demo_packs.py [--verify]     -> assets/packs/*.png
Then python scripts/print_sheet.py -> assets/packs/PRINT_ME.pdf. Print on plain white paper; photograph the paper (NOT a screen:
moire breaks QR decoding). --verify runs vision.verify_photo on every pack and checks the expected verdict."""
import os, sys, zlib, qrcode
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

# name -> dict(batch, printed manufacturer override or None, symbol qr|dm, payload override, blur radius, crop box, expected verdict, note)
# expected = (verdict, must-appear note or None); "QR_FAIL" packs expect the bot to send the user to manual entry.
PACKS = {
    "AX2291": dict(batch="AX2291", expect=("RED", None), note="NSQ match -> RED"),
    "EN3302": dict(batch="EN3302", expect=("EXPIRED", None), note="expired -> EXPIRED"),
    "MQ7756": dict(batch="MQ7756", expect=("GREEN", None), note="clean -> GREEN"),
    "GH6625": dict(batch="GH6625", printed="Zenith Labs Pvt. Ltd.", expect=("AMBER", None), note="label mismatch only -> AMBER"),
    "CT5510": dict(batch="CT5510", printed="Alpha Remedies Ltd.", expect=("RED", None), note="label mismatch + replay flag -> RED (escalated)"),
    "BQ7743": dict(batch="BQ7743", printed="Kestrel Lifesciences Pvt. Ltd.", expect=("RED", None), note="label mismatch + replay flag -> RED (escalated)"),
    "LP2098": dict(batch="LP2098", blur=3.0, expect=("AMBER", "BLUR"), note="mild blur, code still decodes -> AMBER (visual only)"),
    "NS3340_DM": dict(batch="NS3340", symbol="dm", expect=("GREEN", None), note="DataMatrix decodes -> same verdict as its batch (GREEN)"),
    "MQ7756_CROP": dict(batch="MQ7756", crop=(0, 0, 1000, 1275), expect=("AMBER", "CROP"), note="code touches the photo edge -> AMBER (visual only)"),
    "MALFORMED": dict(batch="AX2291", payload="HTTP://FAKE-VERIFY.EXAMPLE/AX2291", expect=(None, "QR_FAIL"), note="not a GS1 code -> 'type the batch number'"),
}


def font(sz):
    for f in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(f, sz)
        except Exception:
            pass
    return ImageFont.load_default()


def _symbol(payload, kind):
    if kind == "dm":
        import zxingcpp
        arr = np.array(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.DataMatrix, payload, 0, 0, quiet_zone=40))
        return Image.fromarray(arr).convert("RGB")
    qr = qrcode.QRCode(box_size=14, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def make(name, spec, out_dir):
    batch = spec["batch"]
    r = db.one("""SELECT b.batch_number, b.expiry_date, p.product_code, p.brand_name, p.generic_name, p.manufacturer
                  FROM batches b JOIN products p USING (product_code) WHERE b.batch_number = %s""", (batch,))
    serial = zlib.crc32(batch.encode()) % 10**8          # deterministic (Python's hash() is randomised per run)
    payload = spec.get("payload") or f"(01){r['product_code'].zfill(14)}(10){batch}(17){r['expiry_date']:%y%m%d}(21)SN{serial:08d}"
    qimg = _symbol(payload, spec.get("symbol", "qr"))
    W, H = 1000, 1450
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([20, 20, W - 20, H - 20], outline="black", width=6)
    d.text((60, 60), r["brand_name"], fill="black", font=font(80))
    d.text((60, 170), r["generic_name"], fill="black", font=font(40))
    y = 300
    d.text((60, y), "Mfd. by: " + (spec.get("printed") or r["manufacturer"]), fill="black", font=font(46))
    d.text((60, y + 90), f"Batch No.: {batch}", fill="black", font=font(56))
    d.text((60, y + 180), f"Exp.: {r['expiry_date']:%m/%Y}", fill="black", font=font(56))
    im.paste(qimg.resize((640, 640)), (180, 700))
    d.text((60, 1380), "SAMPLE PACK - HAMSA DEMO - NOT A REAL MEDICINE", fill="black", font=font(28))
    if spec.get("blur"): im = im.filter(ImageFilter.GaussianBlur(spec["blur"]))
    if spec.get("crop"): im = im.crop(spec["crop"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.png")
    im.save(path)
    return path


def verify(paths, quiet=False):
    """vision.verify_photo on every generated pack. Returns the list of failures (empty = all OK)."""
    import vision
    bad = []
    for name, path in paths.items():
        v, _rec, notes = vision.verify_photo(open(path, "rb").read())
        want_v, want_note = PACKS[name]["expect"]
        ok = (want_v is None or v.verdict == want_v) and (want_note is None or want_note in notes)
        if name in ("CT5510", "BQ7743"): ok = ok and v.reason == "escalated_conflict"
        if not quiet: print(f"{'OK  ' if ok else 'FAIL'} {name:12} -> {v.verdict:8} {v.reason or '':20} notes={notes}")
        if not ok: bad.append(name)
    return bad


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "packs")
    made = {}
    for n, spec in PACKS.items():
        made[n] = make(n, spec, out)
        print(made[n], "->", spec["note"])
    if "--verify" in sys.argv:
        sys.exit(1 if verify(made) else 0)
