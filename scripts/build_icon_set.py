"""Render brand/sf_icon_dark.svg into the full icon set (TimeTrax #902).

Playwright/Chromium renders the SVG at 1024 px (so gradients and the note cut-out are exact),
then Pillow downsamples with LANCZOS for the small sizes and packs a multi-resolution .ico.

Outputs into StudioFire/brand/:
    sf_icon_dark_1024.png, _512, _256, _128, _64, _48, _32, _16
    sf_icon_dark.ico           (16,24,32,48,64,128,256)
Run:  python scripts/build_icon_set.py
"""
import io
import os

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAND = os.path.join(ROOT, "brand")
SVG = os.path.join(BRAND, "sf_icon_dark.svg")
MASTER = os.path.join(BRAND, "_master_1024.png")

with open(SVG, encoding="utf-8") as f:
    svg_text = f.read()

html = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<style>html,body{margin:0;padding:0;background:transparent}"
    "svg{display:block}</style></head><body>" + svg_text + "</body></html>"
)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1024, "height": 1024}, device_scale_factor=1)
    pg.set_content(html)
    pg.screenshot(path=MASTER, omit_background=True)
    b.close()

master = Image.open(MASTER).convert("RGBA")
print("master:", master.size)

sizes = [1024, 512, 256, 128, 64, 48, 32, 16]
made = []
for s in sizes:
    if s == 1024:
        img = master
    else:
        img = master.resize((s, s), Image.LANCZOS)
    out = os.path.join(BRAND, f"sf_icon_dark_{s}.png")
    img.save(out)
    made.append(out)

ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
ico_path = os.path.join(BRAND, "sf_icon_dark.ico")
master.save(ico_path, format="ICO", sizes=ico_sizes)

for path in made + [ico_path]:
    print(" ", os.path.basename(path), os.path.getsize(path) // 1024, "KB")
