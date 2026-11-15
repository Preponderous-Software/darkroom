import random

import pytest
from PIL import Image

from darkroom import encode as enc


def noisy(w, h, seed=0):
    rnd = random.Random(seed)
    return Image.frombytes("RGB", (w, h), bytes(rnd.getrandbits(8) for _ in range(w * h * 3)))


def scene(w, h, i):
    im = Image.new("RGB", (w, h), (30, 90, 40))
    for k in range(40):
        x = (k * 37 + i * 9) % w
        im.paste((200, 20, 30), (x, (k * 23) % h, x + 12, (k * 23) % h + 12))
    return im


def test_jpeg_fits_budget(tmp_path):
    r = enc.encode_still(noisy(800, 450), tmp_path / "a.jpg", budget_kb=60)
    assert r.bytes <= 60 * 1024 and not r.over_budget
    assert r.notes  # it had to step quality and/or width down


def test_jpeg_small_image_untouched(tmp_path):
    r = enc.encode_still(Image.new("RGB", (320, 180), (10, 20, 30)), tmp_path / "a.jpg", budget_kb=200)
    assert r.notes == []


def test_png_quantizes_to_fit(tmp_path):
    r = enc.encode_still(noisy(300, 200), tmp_path / "a.png", budget_kb=60, fmt="png")
    assert r.bytes <= 60 * 1024
    assert any("colours" in n for n in r.notes)


@pytest.mark.parametrize("palette", ["octree", "median", "adaptive"])
def test_gif_palettes(tmp_path, palette):
    frames = [scene(320, 180, i) for i in range(6)]
    r = enc.encode_gif(frames, tmp_path / "a.gif", budget_mb=3, fps=10, palette=palette)
    with Image.open(r.path) as g:
        assert g.n_frames == 6 and g.size == (320, 180)


def test_octree_keeps_red(tmp_path):
    # Small saturated details over a dominant colour survive the octree palette.
    frames = [scene(320, 180, 0)]
    r = enc.encode_gif(frames, tmp_path / "a.gif", budget_mb=3, palette="octree", colors=16)
    with Image.open(r.path) as g:
        px = g.convert("RGB").getpixel((5, 5))
    assert px[0] > 150 and px[1] < 80


def test_gif_budget_fitting_reports(tmp_path):
    frames = [noisy(400, 240, i) for i in range(8)]
    r = enc.encode_gif(frames, tmp_path / "a.gif", budget_mb=0.25, fps=12, width=400)
    assert r.bytes <= 0.25 * 1024 * 1024
    assert any("colours" in n for n in r.notes)
    assert any("width" in n or "frames" in n for n in r.notes)


def test_crop_aspect():
    assert enc.crop_aspect(Image.new("RGB", (1280, 720)), "1:1").size == (720, 720)
    assert enc.crop_aspect(Image.new("RGB", (400, 400)), "16:9").size == (400, 225)


def test_contact_sheet_and_pair(tmp_path):
    frames = [scene(320, 180, i) for i in range(30)]
    r = enc.contact_sheet(frames, tmp_path / "s.png", cols=4, thumb_width=160, max_frames=12)
    with Image.open(r.path) as s:
        assert s.width == 4 * 160 + 5 * 4
    combo = enc.side_by_side(frames[0], frames[5], ("before", "after"), notes=["a note"])
    assert combo.width == 2 * 320 + 8 and combo.height > 180


@pytest.mark.skipif(enc.find_ffmpeg() is None, reason="no ffmpeg")
@pytest.mark.parametrize("fmt", ["mp4", "webm"])
def test_video(tmp_path, fmt):
    frames = [scene(321, 181, i) for i in range(10)]  # odd sizes: must be evened
    r = enc.encode_video(frames, tmp_path / f"a.{fmt}", fmt, budget_kb=600, fps=10)
    assert r.bytes > 0 and r.path.exists()
