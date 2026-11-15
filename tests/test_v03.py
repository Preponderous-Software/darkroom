"""0.3: the inter-frame GIF optimizer, split capture/encode hashes, subsampling, holds,
PNG colours, --out, and the gallery's code-aware markers and per-shot options."""
import io
import json
import random
import re
import shutil
import subprocess

import pytest
from PIL import Image, ImageSequence

from darkroom import __version__
from darkroom import encode as enc
from darkroom import gallery
from darkroom.backends.web import SCREENSHOT_TIMEOUT_MS, action_timeout_ms, template_steps
from darkroom.cli import main
from darkroom.manifest import Shot, load, validate
from darkroom.shoot import shoot


def quiet(*_):
    pass


def static_clip(n=30, size=(480, 270), seed=3):
    """A mostly static clip: a detailed background, a small moving square near the bottom, and
    a HUD counter in the top-left corner that changes every frame. The changes span the whole
    frame, so cropping to their bounding box (all the 0.2 writer did) saves nothing."""
    rng = random.Random(seed)
    bg = Image.new("RGB", size)
    bg.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(size[0] * size[1])])
    bg = bg.resize((size[0] // 4, size[1] // 4)).resize(size, Image.NEAREST)  # blocky detail
    frames = []
    for k in range(n):
        f = bg.copy()
        f.paste((240, 30, 30), (20 + k * 12, size[1] - 50, 60 + k * 12, size[1] - 10))
        f.paste((255, 255, 255) if k % 2 else (0, 0, 0), (4, 4, 4 + 8 * (k % 5 + 1), 12))
        frames.append(f)
    return frames


# ------------------------------------------------------------------ GIF optimizer

@pytest.mark.parametrize("palette", ["adaptive", "octree", "median"])
def test_optimized_gif_shows_exactly_the_quantized_frames(palette):
    frames = static_clip(12)
    data = enc._gif_bytes(frames, 10, palette, 64, False, 0)
    want = enc._quantize_frames(frames, palette, 64, False)
    with Image.open(io.BytesIO(data)) as g:
        got = [f.convert("RGB").tobytes() for f in ImageSequence.Iterator(g)]
    assert got == [w.convert("RGB").tobytes() for w in want]


def test_optimized_gif_is_much_smaller_on_a_mostly_static_clip():
    frames = static_clip(30)
    for palette in ("adaptive", "octree"):
        old = enc._gif_bytes(frames, 12, palette, 256, False, 0, optimize=False)  # the 0.2 writer
        new = enc._gif_bytes(frames, 12, palette, 256, False, 0)
        assert len(new) < len(old) * 0.5, (palette, len(old), len(new))


def test_optimized_gif_structure():
    """Global palette for adaptive, local full palettes per frame otherwise; disposal 1;
    later frames cropped to their changes."""
    frames = static_clip(4)
    data = enc._gif_bytes(frames, 10, "adaptive", 64, False, 0)
    assert data[10] & 0x80 and data[10] & 7 == 7  # a full 256-entry global table
    assert b"\x2C" in data and data.endswith(b"\x3B")
    with Image.open(io.BytesIO(data)) as g:
        g.seek(1)
        assert g.disposal_method == 1
        assert g.dispose_extent[2] - g.dispose_extent[0] < frames[0].width  # cropped
    per_frame = enc._gif_bytes(frames, 10, "octree", 64, False, 0)
    assert not per_frame[10] & 0x80  # no global table: each frame carries its own


@pytest.mark.skipif(not shutil.which("convert"), reason="ImageMagick not installed")
@pytest.mark.parametrize("palette", ["adaptive", "octree"])
def test_optimized_static_gif_decodes_in_imagemagick(tmp_path, palette):
    out = tmp_path / "s.gif"
    enc.encode_gif(static_clip(10), out, budget_mb=3, fps=8, width=480, palette=palette, colors=128)
    r = subprocess.run(["convert", str(out), "-coalesce", str(tmp_path / "f_%02d.png")],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "invalid" not in r.stderr, r.stderr
    assert len(list(tmp_path.glob("f_*.png"))) == 10


def test_identical_frames_merge_and_hold_last():
    frames = static_clip(3) + [static_clip(3)[-1]] * 2
    data = enc._gif_bytes(frames, 10, "adaptive", 64, False, 0, hold_last_ms=1500)
    with Image.open(io.BytesIO(data)) as g:
        durations = [f.info["duration"] for f in ImageSequence.Iterator(g)]
    assert len(durations) == 3
    assert durations[-1] == 100 + 100 + 1500  # merged repeats plus the hold


# ------------------------------------------------------------------ hashes and resume

def clip_manifest(write_manifest, fake_cmd, **extra):
    shot = {"name": "walk", "kind": "clip", "backend": "command", "device": {"width": 160, "height": 90},
            "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--frames", "8"], "gif": {"fps": 8}}
    shot.update(extra)
    return write_manifest({"shots": [shot]})


def test_presentation_fields_do_not_change_hashes(write_manifest, fake_cmd):
    p = clip_manifest(write_manifest, fake_cmd)
    m = load(p)
    base = m.shots[0]
    cap, encode = m.capture_hash(base, None), m.encode_hash(base, None)
    for k, v in {"caption": "x", "alt": "y", "retries": 5, "lock": "rig", "timeout": 900,
                 "comment": "z", "gallery": {"width": 300}, "failOnConsoleError": True}.items():
        s = Shot(dict(base.raw, **{k: v}), 0)
        assert (m.capture_hash(s, None), m.encode_hash(s, None)) == (cap, encode), k
    s = Shot(dict(base.raw, gif={"fps": 8, "colors": 64}), 0)
    assert m.capture_hash(s, None) == cap and m.encode_hash(s, None) != encode
    s = Shot(dict(base.raw, argv=base.raw["argv"] + ["--color", "1,2,3"]), 0)
    assert m.capture_hash(s, None) != cap


def test_encode_change_reencodes_without_reshooting(write_manifest, fake_cmd):
    p = clip_manifest(write_manifest, fake_cmd)
    assert shoot(load(p), log=quiet) == 0
    meta_path = p.parent / ".darkroom/takes/walk/meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta["darkroom"] == __version__ and meta["capture_hash"]
    state = json.loads((p.parent / ".darkroom/state.json").read_text())
    assert state["walk"]["darkroom"] == __version__
    first_take = meta_path.read_text()

    data = json.loads(p.read_text())
    data["shots"][0]["caption"] = "Walking."
    data["shots"][0]["retries"] = 4
    p.write_text(json.dumps(data))
    logs = []
    assert shoot(load(p), log=logs.append) == 0
    assert logs == ["= walk: up to date (use --force to reshoot)"]

    data["shots"][0]["gif"] = {"fps": 8, "colors": 32}
    p.write_text(json.dumps(data))
    logs = []
    assert shoot(load(p), log=logs.append) == 0
    assert "re-encoding the saved take" in logs[0]
    assert meta_path.read_text() == first_take  # not reshot


def test_state_from_0_2_is_adopted_not_reshot(write_manifest, fake_cmd):
    p = clip_manifest(write_manifest, fake_cmd)
    assert shoot(load(p), log=quiet) == 0
    # Rewrite state and take as 0.2 left them: a whole-shot hash, no capture_hash.
    m = load(p)
    state_path = p.parent / ".darkroom/state.json"
    state = json.loads(state_path.read_text())
    state["walk"] = {"hash": m.legacy_hash(m.shots[0], None, "0.2.0"), "outputs": state["walk"]["outputs"],
                     "tag": None, "when": "2026-11-15T00:00:00"}
    state_path.write_text(json.dumps(state))
    meta_path = p.parent / ".darkroom/takes/walk/meta.json"
    meta = json.loads(meta_path.read_text())
    del meta["capture_hash"], meta["darkroom"]
    meta_path.write_text(json.dumps(meta))
    logs = []
    assert shoot(load(p), log=logs.append) == 0
    assert logs == ["= walk: up to date (use --force to reshoot)"]
    assert json.loads(meta_path.read_text())["capture_hash"]  # stamped, so 0.3 resumes from it
    logs = []
    assert shoot(load(p), log=logs.append) == 0
    assert logs == ["= walk: up to date (use --force to reshoot)"]


# ------------------------------------------------------------------ every, holds, colours

def test_every_subsamples_gif_and_video_separately(write_manifest, fake_cmd):
    p = clip_manifest(write_manifest, fake_cmd, every=2, video={"formats": ["mp4"], "every": 1, "hold_last": 1.0})
    assert shoot(load(p), log=quiet) == 0
    with Image.open(p.parent / "media/walk.gif") as g:
        assert g.n_frames == 4
    mp4 = p.parent / "media/walk.mp4"
    if mp4.exists():  # with ffmpeg: 8 frames at 8 fps plus a 1 s hold
        ff = enc.find_ffmpeg()
        r = subprocess.run([ff, "-i", str(mp4)], capture_output=True, text=True)
        h, mnt, sec = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr).groups()
        assert 1.8 <= float(sec) <= 2.3


def test_gif_every_and_hold_last_ms(write_manifest, fake_cmd):
    p = clip_manifest(write_manifest, fake_cmd, gif={"fps": 8, "every": 4, "hold_last_ms": 2000})
    assert shoot(load(p), log=quiet) == 0
    with Image.open(p.parent / "media/walk.gif") as g:
        durations = [f.info["duration"] for f in ImageSequence.Iterator(g)]
    assert len(durations) == 2 and durations[-1] == 2000


def test_png_colors(write_manifest, fake_cmd):
    p = write_manifest({"shots": [{"name": "a", "backend": "command", "format": "png", "colors": 4,
                                   "device": {"width": 120, "height": 80},
                                   "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]}]})
    assert shoot(load(p), log=quiet) == 0
    with Image.open(p.parent / "media/a.png") as im:
        assert len(im.convert("RGB").getcolors(256)) <= 4


# ------------------------------------------------------------------ --out and --only

def test_shoot_out_dir_for_review(write_manifest, fake_cmd, tmp_path):
    p = write_manifest({"shots": [
        {"name": "a", "backend": "command", "device": {"width": 32, "height": 32},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]},
        {"name": "b", "backend": "command", "device": {"width": 32, "height": 32}, "file": "docs/b.png",
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]}]})
    review = tmp_path / "review"
    assert main(["shoot", str(p), "--out", str(review)]) == 0
    assert (review / "a.jpg").exists() and (review / "b.png").exists()
    assert not (p.parent / "media").exists() and not (p.parent / "docs/b.png").exists()
    # The real shoot reuses the takes: nothing is reshot, only encoded.
    logs = []
    assert shoot(load(p), log=logs.append) == 0
    assert all("re-encoding the saved take" in l for l in logs if l.startswith(">"))
    assert (p.parent / "media/a.jpg").exists() and (p.parent / "docs/b.png").exists()


def test_repeated_only_adds_up(write_manifest, fake_cmd, capsys):
    p = write_manifest({"shots": [
        {"name": n, "backend": "command", "device": {"width": 16, "height": 16},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]} for n in ("a", "b", "c")]})
    assert main(["shoot", str(p), "--only", "a", "--only", "b"]) == 0
    assert (p.parent / "media/a.jpg").exists() and (p.parent / "media/b.jpg").exists()
    assert not (p.parent / "media/c.jpg").exists()


# ------------------------------------------------------------------ web job building

def test_screenshot_timeout_defaults():
    assert action_timeout_ms(Shot({"name": "a"}, 0)) == SCREENSHOT_TIMEOUT_MS == 120000
    assert action_timeout_ms(Shot({"name": "a", "timeout": 900}, 0)) == 900000
    assert action_timeout_ms(Shot({"name": "a", "timeout": 900, "screenshotTimeout": 5000}, 0)) == 5000


def test_goto_urls_are_templated():
    steps = template_steps([{"goto": "{base}b.html"}, {"repeat": 2, "steps": [{"goto": "{base}c"}]},
                            {"js": "x = {a: 1}"}], {"base": "http://h/"})
    assert steps[0]["goto"] == "http://h/b.html" and steps[1]["steps"][0]["goto"] == "http://h/c"
    assert steps[2]["js"] == "x = {a: 1}"


# ------------------------------------------------------------------ validation

def errors(shot):
    return validate({"shots": [dict({"name": "s", "url": "http://x/"}, **shot)]})


def test_new_steps_validate():
    assert errors({"kind": "clip", "steps": [
        {"until": "done", "max": 5, "then": 1, "steps": [{"capture": True}]},
        {"goto": "page2.html"}, {"reload": True}, {"tap": "#start"}, {"tap": [1, 2]}]}) == []
    assert errors({"kind": "still", "steps": [{"capture": "title"}, {"capture": "menu"}]}) == []
    assert errors({"adapter": "strata-kit", "frameStep": 0.05, "steps": [{"frames": 3, "step": 0.1}]}) == []
    e = errors({"steps": [{"until": "done", "steps": []}]})
    assert any("steps[0].max: required, a positive integer" in x for x in e)
    e = errors({"kind": "clip", "steps": [{"capture": "title"}]})
    assert any("a named capture ('title') makes a still file" in x for x in e)
    e = errors({"steps": [{"capture": "a b"}]})
    assert any("'a b' is not a file name" in x for x in e)
    e = errors({"steps": [{"frames": 3, "step": 0.1}], "frameStep": 0.1})
    assert any("steps[0].step: only the strata-kit adapter" in x for x in e)
    assert any("frameStep: only the strata-kit adapter" in x for x in e)
    e = errors({"steps": [{"reload": 1}, {"tap": 5}]})
    assert any("reload: must be true" in x for x in e) and any("or a CSS selector" in x for x in e)
    e = errors({"each": ["a", "b"], "steps": [{"capture": "x"}]})
    assert any("named captures and 'each' don't mix" in x for x in e)


def test_new_options_validate():
    assert errors({"kind": "clip", "every": 2, "gif": {"every": 3, "hold_last_ms": 1000},
                   "video": {"formats": ["mp4"], "every": 1, "hold_last": 1.5},
                   "gallery": {"width": 480, "poster": True}, "screenshotTimeout": 300000}) == []
    assert errors({"format": "png", "colors": 16}) == []
    assert any("only PNG stills are quantized" in x for x in errors({"colors": 16}))
    assert any("every: only clips" in x for x in errors({"every": 2}))
    assert any("gallery: unknown key 'widht' (did you mean 'width'?)" in x
               for x in errors({"gallery": {"widht": 3}}))
    assert any("video.hold_last: must be seconds" in x
               for x in errors({"kind": "clip", "video": {"formats": ["mp4"], "hold_last": "1s"}}))
    assert any("screenshotTimeout: must be a positive number" in x for x in errors({"screenshotTimeout": 0}))


# ------------------------------------------------------------------ gallery

def shot_clip_with_poster(write_manifest, fake_cmd, gallery_opts):
    p = clip_manifest(write_manifest, fake_cmd, gallery=gallery_opts, alt="Walking")
    assert shoot(load(p), log=quiet) == 0
    return p


def test_markers_in_code_are_ignored(write_manifest, fake_cmd):
    p = clip_manifest(write_manifest, fake_cmd)
    assert shoot(load(p), log=quiet) == 0
    doc = p.parent / "README.md"
    text = ("Write `<!-- darkroom:shot NAME /-->` to place a shot.\n\n"
            "```markdown\n<!-- darkroom:shot example -->\n<!-- darkroom:end -->\n<!-- darkroom:start -->\n```\n\n"
            "~~~\n<!-- darkroom:shot other /-->\n~~~\n\n"
            "Real: <!-- darkroom:shot walk /-->\n\nAnd ``<!-- darkroom:end -->`` here.\n")
    doc.write_text(text)
    assert main(["gallery", str(p), "--insert", str(doc)]) == 0
    out = doc.read_text()
    assert "`<!-- darkroom:shot NAME /-->`" in out
    assert "```markdown\n<!-- darkroom:shot example -->\n<!-- darkroom:end -->\n<!-- darkroom:start -->\n```" in out
    assert "<!-- darkroom:shot other /-->" in out
    assert "Real: <!-- darkroom:shot walk -->\n![" in out
    assert "``<!-- darkroom:end -->``" in out


def test_gallery_width_and_poster_beside_gif(write_manifest, fake_cmd):
    p = shot_clip_with_poster(write_manifest, fake_cmd, {"width": 320, "poster": True})
    assert (p.parent / "media/walk-poster.jpg").exists()  # gallery.poster makes one
    md = gallery.markdown(load(p), p.parent / "README.md")
    line = next(l for l in md.splitlines() if l.startswith("<img"))
    assert line.count("<img ") == 2 and 'width="320"' in line
    assert line.index("walk-poster.jpg") < line.index("walk.gif")
    page = gallery.html_page(load(p), p.parent / "media/index.html")
    assert 'width="320"' in page and "walk-poster.jpg" in page


def test_gallery_plain_without_options(write_manifest, fake_cmd):
    p = shot_clip_with_poster(write_manifest, fake_cmd, None)
    md = gallery.markdown(load(p), p.parent / "README.md")
    assert "![Walking](media/walk.gif)" in md and "<img" not in md
