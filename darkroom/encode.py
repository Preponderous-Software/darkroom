"""Encoders: budget-fitted JPEG/PNG stills, GIFs, MP4/WebM, posters, contact sheets, pairs.

Everything here is Pillow, except video, which needs an ffmpeg binary (PATH, or the one the
optional ``imageio-ffmpeg`` package ships).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def font(size: int, bold: bool = False):
    for path in (BOLD_CANDIDATES if bold else []) + FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


@dataclass
class Result:
    path: Path
    bytes: int
    notes: list[str] = field(default_factory=list)
    over_budget: bool = False

    def describe(self) -> str:
        size = f"{self.bytes / 1024:.0f} KB" if self.bytes < 1024 * 1024 else f"{self.bytes / 1048576:.2f} MB"
        extra = f" ({'; '.join(self.notes)})" if self.notes else ""
        warn = " OVER BUDGET" if self.over_budget else ""
        return f"{self.path.name}: {size}{extra}{warn}"


def load_rgb(path) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("RGB")


def resize_width(im: Image.Image, width: int | None) -> Image.Image:
    if not width or im.width <= width:
        return im
    h = max(1, round(im.height * width / im.width))
    return im.resize((width, h), Image.LANCZOS)


def crop_aspect(im: Image.Image, aspect: str | None) -> Image.Image:
    """Centre-crop to an aspect like '16:9' (the trailer recorder's ``aspect``)."""
    if not aspect:
        return im
    aw, ah = (int(x) for x in aspect.split(":"))
    target = aw / ah
    w, h = im.size
    if w / h > target:
        nw = round(h * target)
        x = (w - nw) // 2
        return im.crop((x, 0, x + nw, h))
    nh = round(w / target)
    y = (h - nh) // 2
    return im.crop((0, y, w, y + nh))


# ------------------------------------------------------------------ stills

def encode_still(im: Image.Image, out: Path, budget_kb: float, fmt: str = "jpg",
                 quality: int = 86, width: int | None = None) -> Result:
    """JPEG: step quality down (to 40), then shrink width by 15% steps, until under budget.
    PNG: optimize; if too big, quantize to 256/128/64 colours, then shrink."""
    out.parent.mkdir(parents=True, exist_ok=True)
    im = resize_width(im.convert("RGB"), width)
    budget = budget_kb * 1024
    notes: list[str] = []
    if fmt == "png":
        attempts = [(None, im)]
        for colors in (256, 128, 64):
            attempts.append((colors, im))
        cur = im
        for _ in range(4):
            cur = cur.resize((max(1, int(cur.width * .85)), max(1, int(cur.height * .85))), Image.LANCZOS)
            attempts.append((64, cur))
        data = b""
        for colors, src in attempts:
            buf = io.BytesIO()
            img = src if colors is None else src.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
            img.save(buf, "PNG", optimize=True)
            data = buf.getvalue()
            if len(data) <= budget:
                if colors:
                    notes.append(f"{colors} colours")
                if src.width != im.width:
                    notes.append(f"width {src.width}")
                break
        out.write_bytes(data)
        return Result(out, len(data), notes, len(data) > budget)

    cur, q = im, quality
    data = b""
    while True:
        buf = io.BytesIO()
        cur.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
        data = buf.getvalue()
        if len(data) <= budget:
            break
        if q > 40:
            q = max(40, q - 6)
            continue
        if cur.width <= 240:
            break
        cur = cur.resize((int(cur.width * .85), int(cur.height * .85)), Image.LANCZOS)
        q = min(quality, 70)
    if q != quality:
        notes.append(f"quality {q}")
    if cur.width != im.width:
        notes.append(f"width {cur.width}")
    out.write_bytes(data)
    return Result(out, len(data), notes, len(data) > budget)


# ------------------------------------------------------------------ GIF

_METHODS = {"octree": Image.Quantize.FASTOCTREE, "median": Image.Quantize.MEDIANCUT}


def _quantize_frames(frames: list[Image.Image], palette: str, colors: int, dither: bool) -> list[Image.Image]:
    d = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    if palette == "adaptive":
        # One palette for the whole clip, built from a strip of sampled frames: no colour
        # flicker between frames. Octree keeps small saturated details that median cut
        # merges into the dominant colour.
        sample = frames[:: max(1, len(frames) // 8)]
        strip = Image.new("RGB", (sample[0].width, sample[0].height * len(sample)))
        for i, f in enumerate(sample):
            strip.paste(f, (0, i * f.height))
        pal = strip.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
        return [f.quantize(palette=pal, dither=d) for f in frames]
    method = _METHODS[palette]
    return [f.quantize(colors=colors, method=method, dither=d) for f in frames]


def _gif_bytes(frames, fps, palette, colors, dither, loop, hold_last_ms=0) -> bytes:
    q = _quantize_frames(frames, palette, colors, dither)
    ms = max(20, round(1000 / fps))
    durations = [ms] * len(q)
    if hold_last_ms:
        durations[-1] = hold_last_ms
    buf = io.BytesIO()
    q[0].save(buf, "GIF", save_all=True, append_images=q[1:], duration=durations,
              loop=loop, optimize=True, disposal=1)
    return buf.getvalue()


def encode_gif(frames: Sequence[Image.Image], out: Path, budget_mb: float, fps: float = 12,
               width: int | None = 640, palette: str = "octree", colors: int = 256,
               dither: bool = False, loop: int = 0) -> Result:
    """Encode a GIF and fit it under ``budget_mb``: fewer colours (to 64), then narrower
    (by 15% steps, not under 240 px), then every other frame dropped (fps halves, so the
    clip keeps its length). Returns what it did in ``notes``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    budget = budget_mb * 1024 * 1024
    base = [resize_width(f.convert("RGB"), width) for f in frames]
    cur, cur_fps, cur_colors = base, fps, colors
    notes: list[str] = []
    data = _gif_bytes(cur, cur_fps, palette, cur_colors, dither, loop)
    while len(data) > budget:
        if cur_colors > 64:
            cur_colors = max(64, cur_colors // 2)
        elif cur[0].width > 240:
            nw = int(cur[0].width * .85)
            cur = [resize_width(f, nw) for f in cur]
        elif len(cur) > 2:
            cur = cur[::2]
            cur_fps = cur_fps / 2
        else:
            break
        data = _gif_bytes(cur, cur_fps, palette, cur_colors, dither, loop)
    if cur_colors != colors:
        notes.append(f"{cur_colors} colours")
    if base and cur[0].width != base[0].width:
        notes.append(f"width {cur[0].width}")
    if len(cur) != len(base):
        notes.append(f"{len(cur)}/{len(base)} frames at {cur_fps:g} fps")
    out.write_bytes(data)
    return Result(out, len(data), notes, len(data) > budget)


# ------------------------------------------------------------------ video

def find_ffmpeg() -> str | None:
    env = os.environ.get("DARKROOM_FFMPEG") or os.environ.get("FFMPEG")
    if env and Path(env).exists():
        return env
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# CRF ladders: raise the CRF until the file fits (borrowed from the trailer recorder).
CRF_LADDER = {"mp4": [23, 26, 29, 32, 35, 38], "webm": [33, 36, 40, 44, 48]}


def encode_video(frames: Sequence[Image.Image], out: Path, fmt: str, budget_kb: float,
                 fps: float = 30, width: int | None = None) -> Result | None:
    """MP4 (H.264, yuv420p, even dimensions, faststart) or WebM (VP9). Returns None when
    no ffmpeg is available."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    budget = budget_kb * 1024
    with tempfile.TemporaryDirectory(prefix="darkroom-video-") as tmp:
        for i, f in enumerate(frames):
            resize_width(f.convert("RGB"), width).save(Path(tmp) / f"{i:05d}.png", compress_level=1)
        base = [ffmpeg, "-y", "-loglevel", "error", "-framerate", f"{fps:g}", "-i",
                str(Path(tmp) / "%05d.png"), "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-an"]
        crf_used = None
        for crf in CRF_LADDER[fmt]:
            if fmt == "mp4":
                codec = ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
                         "-movflags", "+faststart"]
            else:
                codec = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(crf), "-row-mt", "1",
                         "-pix_fmt", "yuv420p"]
            subprocess.run(base + codec + [str(out)], check=True, capture_output=True)
            crf_used = crf
            if out.stat().st_size <= budget:
                break
    size = out.stat().st_size
    return Result(out, size, [f"crf {crf_used}"], size > budget)


# ------------------------------------------------------------------ sheets and pairs

def contact_sheet(frames: Sequence[Image.Image], out: Path, cols: int = 4, thumb_width: int = 320,
                  labels: Sequence[str] | None = None, max_frames: int = 24) -> Result:
    """A grid of thumbnails, each labelled (frame index by default)."""
    frames = list(frames)
    idx = list(range(len(frames)))
    if len(frames) > max_frames:
        step = (len(frames) - 1) / (max_frames - 1)
        idx = sorted({round(i * step) for i in range(max_frames)})
    thumbs = [resize_width(frames[i].convert("RGB"), thumb_width) for i in idx]
    labels = [labels[i] for i in idx] if labels else [f"#{i}" for i in idx]
    tw = max(t.width for t in thumbs)
    th = max(t.height for t in thumbs)
    bar = 22
    cols = min(cols, len(thumbs))
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * 4, rows * (th + bar) + (rows + 1) * 4), (20, 22, 28))
    d = ImageDraw.Draw(sheet)
    f = font(13)
    for k, (t, lab) in enumerate(zip(thumbs, labels)):
        x = 4 + (k % cols) * (tw + 4)
        y = 4 + (k // cols) * (th + bar + 4)
        sheet.paste(t, (x, y))
        d.text((x + 4, y + th + 4), lab, font=f, fill=(225, 228, 235))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)
    return Result(out, out.stat().st_size)


def side_by_side(left: Image.Image, right: Image.Image, labels=("before", "after"),
                 notes: Sequence[str] = (), gap: int = 8) -> Image.Image:
    """Two images side by side with a caption bar over each and optional note lines below."""
    left, right = left.convert("RGB"), right.convert("RGB")
    w, h = left.size
    if right.size != left.size:
        right = right.resize((w, h), Image.LANCZOS)
    head = max(34, h // 18)
    line = 22
    foot = (len(notes) * line + 12) if notes else 0
    out = Image.new("RGB", (2 * w + gap, h + head + foot), (24, 28, 36))
    out.paste(left, (0, head))
    out.paste(right, (w + gap, head))
    d = ImageDraw.Draw(out)
    fb, fn = font(max(15, head // 2), bold=True), font(15)
    pad = (head - fb.size) // 2 if hasattr(fb, "size") else 8
    d.text((14, pad), labels[0], font=fb, fill=(235, 235, 235))
    d.text((w + gap + 14, pad), labels[1], font=fb, fill=(235, 235, 235))
    for i, note in enumerate(notes):
        d.text((14, h + head + 8 + i * line), note, font=fn, fill=(200, 205, 212))
    return out
