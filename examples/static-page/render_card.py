"""A tiny stand-in for a native game's screenshot flag, used by the command-backend example.

    python render_card.py OUT_DIR WIDTH HEIGHT LABEL [FRAMES]

Writes FRAMES PNGs (default 1) into OUT_DIR, like `game --screenshot-after-frames N` would.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

out, w, h, label = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
frames = int(sys.argv[5]) if len(sys.argv) > 5 else 1
for i in range(frames):
    im = Image.new("RGB", (w, h), (18, 14, 14))
    d = ImageDraw.Draw(im)
    for k in range(12):
        r = (k + 1) * min(w, h) / 26 + i * 3
        d.ellipse([w / 2 - r, h / 2 - r, w / 2 + r, h / 2 + r], outline=(200 - k * 12, 40 + k * 8, 30), width=3)
    d.text((16, 12), f"{label}  {w}x{h}", fill=(240, 220, 200))
    im.save(out / f"{i:04d}.png")
