"""Builds assets/packs/PRINT_ME.pdf from the demo pack PNGs: A4 portrait, 2 packs per page side by side, 300 dpi, no crop marks.
Run after make_demo_packs.py:   python scripts/print_sheet.py
Print at 100% ("actual size") on plain white matte paper, then photograph the PAPER (never a screen)."""
import os, sys
from PIL import Image, ImageDraw
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_demo_packs import PACKS, font

DPI = 300
PAGE_W, PAGE_H = 2480, 3508                    # A4 at 300 dpi
CELL_W = PAGE_W // 2
MARGIN = 60


def build(pack_dir):
    pages = []
    names = list(PACKS)
    for i in range(0, len(names), 2):
        page = Image.new("RGB", (PAGE_W, PAGE_H), "white")
        d = ImageDraw.Draw(page)
        for col, name in enumerate(names[i:i + 2]):
            im = Image.open(os.path.join(pack_dir, name + ".png")).convert("RGB")
            scale = min((CELL_W - 2 * MARGIN) / im.width, (PAGE_H - 2 * MARGIN - 120) / im.height)
            im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
            x = col * CELL_W + (CELL_W - im.width) // 2
            page.paste(im, (x, MARGIN))
            d.text((x, MARGIN + im.height + 30), f"{name}  -  expected: {PACKS[name]['note']}", fill="black", font=font(34))
        pages.append(page)
    out = os.path.join(pack_dir, "PRINT_ME.pdf")
    pages[0].save(out, "PDF", resolution=float(DPI), save_all=True, append_images=pages[1:])
    return out, len(pages)


if __name__ == "__main__":
    pack_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "packs")
    out, n = build(pack_dir)
    print(out, f"({n} pages, {len(PACKS)} packs)")
