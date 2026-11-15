"""Encoders: budget-fitted JPEG/PNG stills, GIFs, MP4/WebM, posters, contact sheets, pairs.

Everything here is Pillow, except video, which needs an ffmpeg binary (PATH, or the one the
optional ``imageio-ffmpeg`` package ships).
"""
from __future__ import annotations

import io
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import procs

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
                 quality: int = 86, width: int | None = None, colors: int | None = None) -> Result:
    """JPEG: step quality down (to 40), then shrink width by 15% steps, until under budget.
    PNG: optimize; if too big, quantize to 256/128/64 colours, then shrink. ``colors`` (PNG
    only) quantizes to at most that many colours from the start."""
    out.parent.mkdir(parents=True, exist_ok=True)
    im = resize_width(im.convert("RGB"), width)
    budget = budget_kb * 1024
    notes: list[str] = []
    if fmt == "png":
        attempts = [(colors, im)]
        for n in (256, 128, 64):
            if colors is None or n < colors:
                attempts.append((n, im))
        cur, floor = im, min(64, colors or 64)
        for _ in range(4):
            cur = cur.resize((max(1, int(cur.width * .85)), max(1, int(cur.height * .85))), Image.LANCZOS)
            attempts.append((floor, cur))
        data = b""
        for n, src in attempts:
            buf = io.BytesIO()
            img = src if n is None else src.quantize(colors=n, method=Image.Quantize.FASTOCTREE)
            img.save(buf, "PNG", optimize=True)
            data = buf.getvalue()
            if len(data) <= budget:
                if n:
                    notes.append(f"{n} colours")
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


# Palette index 255 is never a colour: frames quantize to at most 255 colours, and the
# inter-frame optimizer marks unchanged pixels with this index as transparent.
TRANSPARENT = 255


def _quantize_frames(frames: list[Image.Image], palette: str, colors: int, dither: bool) -> list[Image.Image]:
    d = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    colors = max(2, min(colors, 255))
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


def _full_palette(im: Image.Image) -> bytes:
    """A frame's palette as exactly 256 RGB entries (unused ones black)."""
    pal = bytes(im.getpalette("RGB") or [])[:768]
    return pal + bytes(768 - len(pal))


def _lzw_data(im: Image.Image) -> tuple[bytes, bool]:
    """LZW image data (min code size + sub-blocks + terminator) for a "P" frame whose palette
    has 256 entries, encoded by Pillow as a one-frame GIF and lifted out of it. Pillow is told
    not to optimize, so the colour indices are written as they are. Returns (data, interlaced)."""
    frame = im.copy()
    frame.info.pop("transparency", None)
    frame.putpalette(_full_palette(im), "RGB")
    buf = io.BytesIO()
    frame.save(buf, "GIF", optimize=False, interlace=False)
    b = buf.getvalue()
    pos = 13
    if b[10] & 0x80:
        pos += 3 << ((b[10] & 7) + 1)
    while b[pos] == 0x21:  # extensions: label, then sub-blocks
        pos += 2
        while b[pos]:
            pos += b[pos] + 1
        pos += 1
    if b[pos] != 0x2C:
        raise ValueError("unexpected GIF layout from Pillow")
    packed = b[pos + 9]
    pos += 10
    if packed & 0x80:
        pos += 3 << ((packed & 7) + 1)
    start = pos
    pos += 1  # LZW minimum code size
    while b[pos]:
        pos += b[pos] + 1
    return b[start:pos + 1], bool(packed & 0x40)


def _changed(prev: Image.Image, cur: Image.Image) -> tuple[Image.Image, tuple | None]:
    """A mask ("L", 255 = unchanged) of pixels whose displayed colour did not change, and the
    bounding box of the changed pixels (None when nothing changed)."""
    diff = ImageChops.difference(prev, cur)
    bbox = diff.getbbox()
    r, g, b = diff.split()
    any_change = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return any_change.point([255] + [0] * 255), bbox


def write_gif(q: Sequence[Image.Image], durations: Sequence[int], loop: int | None = 0,
              shared_palette: bool = True) -> bytes:
    """darkroom's own GIF writer with safe inter-frame optimization.

    Every frame after the first keeps only the pixels that changed from the frame before:
    unchanged pixels become the reserved transparent index (255) and the frame is cropped to
    the bounding box of the changes. Frames are not disposed (disposal 1), so each one paints
    over the last. With a shared palette there is one global 256-entry table and no local
    ones; with per-frame palettes every frame carries its own full 256-entry table. Colour
    indices therefore always fall inside the table in force (unlike Pillow's optimizer, which
    trims palettes and can leave indices past the end; strict decoders reject those files).
    A frame identical to the one before is dropped and its time added to the previous one."""
    import struct
    w, h = q[0].size
    out = bytearray(b"GIF89a")
    if shared_palette:
        out += struct.pack("<HHBBB", w, h, 0xF7, 0, 0) + _full_palette(q[0])
    else:
        out += struct.pack("<HHBBB", w, h, 0x70, 0, 0)
    if loop is not None:
        out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01" + struct.pack("<H", loop) + b"\x00"
    # (image, box, transparent?, duration ms[, opaque crop]), merging unchanged frames into
    # the one before.
    frames: list[list] = []
    prev_rgb = None
    for im, ms in zip(q, durations):
        rgb = im.convert("RGB")
        if prev_rgb is None:
            frames.append([im, (0, 0, w, h), False, ms])
        else:
            unchanged, bbox = _changed(prev_rgb, rgb)
            if bbox is None:
                frames[-1][3] += ms
                continue
            delta = im.copy()
            delta.paste(TRANSPARENT, mask=unchanged)
            frames.append([delta.crop(bbox), bbox, True, ms, im.crop(bbox)])
        prev_rgb = rgb
    for im, box, transparent, ms, *opaque in frames:
        data, interlaced = _lzw_data(im)
        if opaque:
            # Transparency usually wins; in busy motion it can break up runs the plain crop
            # compresses better. Keep whichever is smaller (both show the same picture).
            plain, plain_il = _lzw_data(opaque[0])
            if len(plain) < len(data):
                data, interlaced, transparent = plain, plain_il, False
        cs = max(2, round(ms / 10))
        out += b"\x21\xF9\x04" + bytes([(1 << 2) | (1 if transparent else 0)]) + struct.pack("<H", cs)
        out += bytes([TRANSPARENT if transparent else 0, 0])
        packed = (0x40 if interlaced else 0) | (0 if shared_palette else 0x87)
        out += b"\x2C" + struct.pack("<HHHHB", box[0], box[1], im.width, im.height, packed)
        if not shared_palette:
            out += _full_palette(im)
        out += data
    out += b"\x3B"
    return bytes(out)


def _gif_bytes(frames, fps, palette, colors, dither, loop, hold_last_ms=0, optimize=True) -> bytes:
    q = _quantize_frames(frames, palette, colors, dither)
    ms = max(20, round(1000 / fps))
    durations = [ms] * len(q)
    if hold_last_ms:
        durations[-1] = max(durations[-1], int(hold_last_ms))
    if optimize:
        return write_gif(q, durations, loop, shared_palette=(palette == "adaptive"))
    # The 0.2 path, kept for comparison: Pillow without its optimizer (full frames).
    buf = io.BytesIO()
    q[0].save(buf, "GIF", save_all=True, append_images=q[1:], duration=durations,
              loop=loop, optimize=False, disposal=1)
    return buf.getvalue()


def encode_gif(frames: Sequence[Image.Image], out: Path, budget_mb: float, fps: float = 12,
               width: int | None = 640, palette: str = "adaptive", colors: int = 256,
               dither: bool = False, loop: int = 0, hold_last_ms: int = 0) -> Result:
    """Encode a GIF and fit it under ``budget_mb``: fewer colours (to 64), then narrower
    (by 15% steps, not under 240 px), then every other frame dropped (fps halves, so the
    clip keeps its length). ``hold_last_ms`` shows the last frame that long before the loop
    restarts. Returns what it did in ``notes``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    budget = budget_mb * 1024 * 1024
    base = [resize_width(f.convert("RGB"), width) for f in frames]
    cur, cur_fps, cur_colors = base, fps, colors
    notes: list[str] = []
    data = _gif_bytes(cur, cur_fps, palette, cur_colors, dither, loop, hold_last_ms)
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
        data = _gif_bytes(cur, cur_fps, palette, cur_colors, dither, loop, hold_last_ms)
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


# Seconds one ffmpeg encode may take before it is killed (``$DARKROOM_FFMPEG_TIMEOUT``).
FFMPEG_TIMEOUT = float(os.environ.get("DARKROOM_FFMPEG_TIMEOUT", 900))

# CRF ladders: raise the CRF until the file fits (borrowed from the trailer recorder).
CRF_LADDER = {"mp4": [23, 26, 29, 32, 35, 38], "webm": [33, 36, 40, 44, 48]}


def encode_video(frames: Sequence[Image.Image], out: Path, fmt: str, budget_kb: float,
                 fps: float = 30, width: int | None = None, hold_last: float = 0) -> Result | None:
    """MP4 (H.264, yuv420p, even dimensions, faststart) or WebM (VP9). ``hold_last`` (seconds)
    holds the last frame that much longer. Returns None when no ffmpeg is available."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    budget = budget_kb * 1024
    with tempfile.TemporaryDirectory(prefix="darkroom-video-") as tmp:
        for i, f in enumerate(frames):
            resize_width(f.convert("RGB"), width).save(Path(tmp) / f"{i:05d}.png", compress_level=1)
        vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        if hold_last and hold_last > 0:
            vf += f",tpad=stop_mode=clone:stop_duration={float(hold_last):g}"
        base = [ffmpeg, "-y", "-loglevel", "error", "-framerate", f"{fps:g}", "-i",
                str(Path(tmp) / "%05d.png"), "-vf", vf, "-an"]
        crf_used = None
        for crf in CRF_LADDER[fmt]:
            if fmt == "mp4":
                codec = ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
                         "-movflags", "+faststart"]
            else:
                codec = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(crf), "-row-mt", "1",
                         "-pix_fmt", "yuv420p"]
            try:
                procs.run(base + codec + [str(out)], check=True, timeout=FFMPEG_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"ffmpeg did not finish {out.name} within {FFMPEG_TIMEOUT:g}s")
            crf_used = crf
            if out.stat().st_size <= budget:
                break
    size = out.stat().st_size
    return Result(out, size, [f"crf {crf_used}"], size > budget)


# ------------------------------------------------------------------ sheets and pairs

SHEET_WIDTH = 1600


def contact_sheet(frames: Sequence[Image.Image], out: Path, cols: int | None = None,
                  thumb_width: int | None = None, labels: str | Sequence[str | None] = "index",
                  max_frames: int = 24, width: int = SHEET_WIDTH) -> Result:
    """A grid of thumbnails for reviewing a take.

    Tiles are sized from the frames so the sheet is about ``width`` pixels wide (never upscaled
    past a frame's own width); ``thumb_width`` fixes the tile width instead. ``labels`` is
    ``"index"`` (``#3``), ``"none"`` (no label bar), or one string per frame (``None`` entries
    fall back to the index)."""
    frames = list(frames)
    if not frames:
        raise ValueError("contact sheet: no frames")
    idx = list(range(len(frames)))
    if len(frames) > max_frames:
        step = (len(frames) - 1) / (max_frames - 1)
        idx = sorted({round(i * step) for i in range(max_frames)})
    gap = 4
    cols = max(1, min(cols or 4, len(idx)))
    if not thumb_width:
        widest = max(frames[i].width for i in idx)
        thumb_width = max(64, min(widest, (width - (cols + 1) * gap) // cols))
    thumbs = [_fit_width(frames[i].convert("RGB"), thumb_width) for i in idx]
    if labels == "none":
        texts = None
    elif labels == "index" or labels is None:
        texts = [f"#{i}" for i in idx]
    else:
        texts = [labels[i] if i < len(labels) and labels[i] is not None else f"#{i}" for i in idx]
    tw = max(t.width for t in thumbs)
    th = max(t.height for t in thumbs)
    bar = 22 if texts else 0
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * gap, rows * (th + bar) + (rows + 1) * gap), (20, 22, 28))
    d = ImageDraw.Draw(sheet)
    f = font(13)
    for k, t in enumerate(thumbs):
        x = gap + (k % cols) * (tw + gap)
        y = gap + (k // cols) * (th + bar + gap)
        sheet.paste(t, (x, y))
        if texts:
            d.text((x + 4, y + th + 4), texts[k], font=f, fill=(225, 228, 235))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)
    return Result(out, out.stat().st_size)


def _fit_width(im: Image.Image, width: int) -> Image.Image:
    """Resize to exactly ``width`` (up or down), keeping the aspect ratio."""
    if im.width == width:
        return im
    return im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)


GRID_THEMES = {"dark": ((24, 28, 36), (235, 236, 240), (200, 205, 212))}


def grid(images: Sequence[Image.Image], captions: Sequence[str | None] = (), title: str | None = None,
         cols: int | None = None, width: int = SHEET_WIDTH, gap: int = 12) -> Image.Image:
    """A composite for READMEs: tiles in a grid, an optional caption centred under each tile
    and an optional title above. No frame numbers. Tiles share the first image's aspect ratio
    (others are centre-cropped to it) so rows line up."""
    images = [im.convert("RGB") for im in images]
    if not images:
        raise ValueError("grid: no images")
    n = len(images)
    cols = max(1, min(cols or (n if n <= 3 else 2 if n == 4 else math.ceil(math.sqrt(n))), n))
    tw = max(64, min(max(im.width for im in images), (width - (cols + 1) * gap) // cols))
    th = max(1, round(images[0].height * tw / images[0].width))
    ratio = f"{images[0].width}:{images[0].height}"
    tiles = [_fit_width(crop_aspect(im, ratio), tw).resize((tw, th), Image.LANCZOS) for im in images]
    caps = [(captions[i] if i < len(captions) else None) or "" for i in range(n)]
    cap_font = font(max(14, min(28, tw // 18)))
    title_font = font(max(18, min(40, (cols * tw) // 30)), bold=True)
    cap_h = (_text_h(cap_font) + gap) if any(caps) else 0
    title_h = (_text_h(title_font) + 2 * gap) if title else 0
    rows = (n + cols - 1) // cols
    bg, fg, muted = GRID_THEMES["dark"]
    out = Image.new("RGB", (cols * tw + (cols + 1) * gap, title_h + rows * (th + cap_h) + (rows + 1) * gap), bg)
    d = ImageDraw.Draw(out)
    if title:
        d.text((out.width // 2, gap + title_h // 2), title, font=title_font, fill=fg, anchor="mm")
    for k, t in enumerate(tiles):
        x = gap + (k % cols) * (tw + gap)
        y = title_h + gap + (k // cols) * (th + cap_h + gap)
        out.paste(t, (x, y))
        if caps[k]:
            d.text((x + tw // 2, y + th + gap // 2 + _text_h(cap_font) // 2), _ellipsize(d, caps[k], cap_font, tw),
                   font=cap_font, fill=muted, anchor="mm")
    return out


def _text_h(f) -> int:
    box = f.getbbox("Ag")
    return box[3] - box[1] + 4


def _ellipsize(d: ImageDraw.ImageDraw, text: str, f, width: int) -> str:
    if d.textlength(text, font=f) <= width:
        return text
    while text and d.textlength(text + "…", font=f) > width:
        text = text[:-1]
    return text + "…"


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
