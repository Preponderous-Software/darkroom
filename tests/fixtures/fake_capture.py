"""A stand-in for a game binary: writes PNGs into a directory.

    fake_capture.py OUT_DIR WIDTH HEIGHT [--frames N] [--color R,G,B] [--label TEXT]

Writes DISPLAY (if any) to OUT_DIR/../display-<pid>.txt when DARKROOM_TEST_DISPLAY_LOG is set.
"""
import argparse
import os
import time
from pathlib import Path

from PIL import Image, ImageDraw

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("width", type=int)
ap.add_argument("height", type=int)
ap.add_argument("--frames", type=int, default=1)
ap.add_argument("--color", default="40,120,200")
ap.add_argument("--sleep", type=float, default=0)
a = ap.parse_args()
log = os.environ.get("DARKROOM_TEST_DISPLAY_LOG")
if log:
    with open(log, "a") as f:
        f.write(f"{os.environ.get('DISPLAY', '')} {time.time():.3f} start\n")
time.sleep(a.sleep)
c = tuple(int(x) for x in a.color.split(","))
for i in range(a.frames):
    im = Image.new("RGB", (a.width, a.height), c)
    d = ImageDraw.Draw(im)
    x = int((a.width - 40) * i / max(1, a.frames - 1))
    d.rectangle([x, a.height // 3, x + 40, a.height // 3 + 40], fill=(250, 200, 40))
    im.save(Path(a.out) / f"frame-{i:03d}.png")
if log:
    with open(log, "a") as f:
        f.write(f"{os.environ.get('DISPLAY', '')} {time.time():.3f} end\n")
