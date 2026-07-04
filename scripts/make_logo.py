"""Turn media/Images/sf_icon.jpg into a tight, transparent PNG logo for the
top bar. Keys out the near-white background and crops to the artwork.
Run: python scripts/make_logo.py
"""
import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "media", "Images", "sf_icon.jpg")
DST = os.path.join(ROOT, "web", "static", "sf_logo.png")

img = Image.open(SRC).convert("RGBA")
px = img.load()
w, h = img.size

# background = average of the four corners
corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
bg = tuple(sum(c[i] for c in corners) // 4 for i in range(3))

LOW, HIGH = 30, 70  # dist<LOW -> transparent, >HIGH -> opaque, feather between
for y in range(h):
    for x in range(w):
        r, g, b, _ = px[x, y]
        dist = ((r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2) ** 0.5
        if dist <= LOW:
            a = 0
        elif dist >= HIGH:
            a = 255
        else:
            a = int(255 * (dist - LOW) / (HIGH - LOW))
        px[x, y] = (r, g, b, a)

# crop to the non-transparent bounding box (+ small padding)
bbox = img.getbbox()
if bbox:
    pad = 12
    l, t, r, b = bbox
    img = img.crop((max(0, l - pad), max(0, t - pad),
                    min(w, r + pad), min(h, b + pad)))

os.makedirs(os.path.dirname(DST), exist_ok=True)
img.save(DST)
print("wrote", DST, img.size)
