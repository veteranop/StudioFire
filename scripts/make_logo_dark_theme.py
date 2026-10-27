"""Dark-theme recolour of the StudioFire logo (TimeTrax #902 / Mark 2026-10-26).

Keeps the existing artwork exactly as-is and only re-colours it for dark backgrounds:
 - navy/purple elements (equalizer bars, "Studio" wordmark) -> light warm white with a soft
   lavender tint on the taller bar
 - orange elements (flame, "Fire" wordmark)               -> kept, slightly deepened

Outputs (into StudioFire Icon Refresh/):
 - sf_logo_dark.png            transparent background
 - sf_logo_dark_on_dark.png    on #14161a
 - sf_icon_dark_1024.png       square app icon = the mark only, centred on #14161a
 - sf_icon_dark_transparent_1024.png  square icon, transparent bg
"""
import os
import numpy as np
from PIL import Image

ROOT = r"C:\Users\markd\Desktop\Projects"
SRC = os.path.join(ROOT, "StudioFire", "web", "static", "sf_logo.png")
OUT = os.path.join(ROOT, "StudioFire Icon Refresh")
os.makedirs(OUT, exist_ok=True)

LIGHT = np.array([242, 238, 233], dtype=np.float32)   # warm white
DARK_BG = (20, 22, 26)

img = Image.open(SRC).convert("RGBA")
a = np.asarray(img).astype(np.float32)
rgb, alpha = a[..., :3], a[..., 3]

r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
mx, mn = rgb.max(axis=2), rgb.min(axis=2)
sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)

# hue in degrees
hue = np.zeros_like(mx)
denom = np.maximum(mx - mn, 1e-6)
hue = np.where(mx == r, ((g - b) / denom) % 6, hue)
hue = np.where(mx == g, (b - r) / denom + 2, hue)
hue = np.where(mx == b, (r - g) / denom + 4, hue)
hue = hue * 60.0

# Only STRONGLY orange pixels are treated as warm. The tall bar's lower half is a dark maroon
# (r high but g very low) — under a looser test it survives as a muddy red; here it reads cool and
# gets lightened with the rest of the mark.
warm = (r > 150) & (g > 0.35 * r) & (b < 0.45 * r)
cool = ~warm

out = rgb.copy()
# cool elements -> light warm-white, preserving relative luminance (keeps gradients/AA)
lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
cool_lum = lum[cool]
lo, hi = np.percentile(cool_lum, 2), np.percentile(cool_lum, 98)
norm = np.clip((lum - lo) / max(hi - lo, 1e-3), 0.0, 1.0)
scale = 0.74 + 0.26 * norm          # 0.74 -> 1.00, so navy reads as light, not grey
for i in range(3):
    out[..., i] = np.where(cool, LIGHT[i] * scale, out[..., i])
# warm elements -> slightly deeper, richer orange for dark backgrounds
out[..., 0] = np.where(warm, np.clip(r * 1.02, 0, 255), out[..., 0])
out[..., 1] = np.where(warm, np.clip(g * 0.94, 0, 255), out[..., 1])
out[..., 2] = np.where(warm, np.clip(b * 0.92, 0, 255), out[..., 2])

recol = Image.fromarray(np.dstack([out, alpha]).astype(np.uint8), "RGBA")
bbox = recol.getbbox()
recol = recol.crop(bbox)
recol.save(os.path.join(OUT, "sf_logo_dark.png"))

on_dark = Image.new("RGBA", recol.size, DARK_BG + (255,))
on_dark.alpha_composite(recol)
on_dark.convert("RGB").save(os.path.join(OUT, "sf_logo_dark_on_dark.png"))

# --- square app icon: the mark only (bars + flame) ---
# The bars and the flame are separate shapes, and the widest transparent gutter is BETWEEN the bars,
# so a gutter heuristic is unsafe. The flame is the only warm element left of the wordmark: take the
# first contiguous warm block in the left half and end the crop just past it.
arr = np.asarray(recol)
col_has = (arr[..., 3] > 8).any(axis=0)
r_warm = np.asarray(recol)[..., 0].astype(np.int16)
g_warm = np.asarray(recol)[..., 1].astype(np.int16)
b_warm = np.asarray(recol)[..., 2].astype(np.int16)
warm_cols = (((r_warm > 150) & (g_warm > 0.35 * r_warm) & (b_warm < 0.45 * r_warm))
             & (arr[..., 3] > 8)).any(axis=0)

limit = int(recol.width * 0.62)
flame_start = next((x for x in range(limit) if warm_cols[x]), None)
flame_end = None
if flame_start is not None:
    gap = 0
    for x in range(flame_start, limit):
        if warm_cols[x]:
            flame_end, gap = x, 0
        else:
            gap += 1
            if gap > max(4, int(recol.width * 0.008)):
                break
# hard cap: the mark cannot be more than 40% of the lockup width, whatever the mask says
mark_w = int(recol.width * 0.34)
if flame_end:
    mark_w = min(flame_end + 1, mark_w)
mark = recol.crop((0, 0, min(mark_w, recol.width), recol.height))

for name, bg in (("sf_icon_dark_1024.png", DARK_BG), ("sf_icon_dark_transparent_1024.png", None)):
    side = 1024
    inner = int(side * 0.76)
    sc = min(inner / mark.width, inner / mark.height)
    m = mark.resize((max(1, int(mark.width * sc)), max(1, int(mark.height * sc))), Image.LANCZOS)
    canvas = Image.new("RGBA", (side, side), (bg + (255,)) if bg else (0, 0, 0, 0))
    canvas.alpha_composite(m, ((side - m.width) // 2, (side - m.height) // 2))
    canvas.save(os.path.join(OUT, name))

print("wrote to", OUT)
for f in sorted(os.listdir(OUT)):
    print(" ", f, os.path.getsize(os.path.join(OUT, f)) // 1024, "KB")
