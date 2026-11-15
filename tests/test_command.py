import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from darkroom import gallery
from darkroom.cli import main
from darkroom.locks import LockTimeout, lock
from darkroom.manifest import load
from darkroom.shoot import shoot


def cmd_manifest(write_manifest, fake_cmd, **extra):
    shots = [
        {"name": "title", "kind": "still", "backend": "command", "device": {"width": 320, "height": 180},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--color", "{color}"],
         "caption": "The title screen.", "alt": "A blue title screen"},
        {"name": "walk", "kind": "clip", "backend": "command", "device": {"width": 320, "height": 180},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--frames", "8"],
         "gif": {"fps": 8, "palette": "adaptive"}, "video": ["mp4"], "caption": "Walking."},
    ]
    data = {"out": "docs/media", "vars": {"color": "40,120,200"}, "shots": shots}
    data.update(extra)
    return write_manifest(data)


def test_command_still_and_clip(write_manifest, fake_cmd):
    p = cmd_manifest(write_manifest, fake_cmd)
    m = load(p)
    logs = []
    assert shoot(m, log=logs.append) == 0
    out = p.parent / "docs/media"
    assert (out / "title.jpg").exists()
    with Image.open(out / "walk.gif") as g:
        assert g.n_frames == 8
    assert (out / "walk-poster.jpg").exists()  # video implies a poster
    meta = json.loads((p.parent / ".darkroom/takes/walk/meta.json").read_text())
    assert len(meta["frames"]) == 8
    # resume: nothing to do the second time
    logs.clear()
    assert shoot(m, log=logs.append) == 0
    assert all("up to date" in l for l in logs if l.startswith(("=", ">")))
    # a var change invalidates
    logs.clear()
    shoot(load(p, {"color": "200,40,40"}), only=["title"], log=logs.append)
    assert any(l.startswith("> title") for l in logs)


def test_failure_does_not_stop_others(write_manifest, fake_cmd):
    p = write_manifest({"shots": [
        {"name": "bad", "backend": "command", "argv": [sys.executable, "-c", "raise SystemExit(3)"]},
        {"name": "good", "backend": "command", "device": {"width": 64, "height": 64},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]},
    ]})
    logs = []
    assert shoot(load(p), retries=1, log=logs.append) == 1
    assert sum("attempt " in l and "failed" in l for l in logs) == 2
    assert (p.parent / "media/good.jpg").exists()
    assert main(["shoot", str(p), "--only", "bad", "--retries", "0"]) == 1


def test_encode_only_reencodes(write_manifest, fake_cmd):
    p = cmd_manifest(write_manifest, fake_cmd)
    m = load(p)
    shoot(m, only=["walk"])
    gif = p.parent / "docs/media/walk.gif"
    gif.unlink()
    assert shoot(m, only=["walk"], encode_only=True) == 0
    assert gif.exists()


def test_tags_and_pair(write_manifest, fake_cmd):
    p = cmd_manifest(write_manifest, fake_cmd)
    data = json.loads(p.read_text())
    data["shots"].append({"name": "title-compare", "kind": "pair", "of": "title", "caption": "Old vs new."})
    p.write_text(json.dumps(data))
    m = load(p)
    assert shoot(m, only=["title"], tag="before") == 0
    assert shoot(load(p, {"color": "200,60,40"}), only=["title"], tag="after") == 0
    assert (p.parent / "docs/media/title.before.jpg").exists()
    assert shoot(m, only=["title-compare"]) == 0
    with Image.open(p.parent / "docs/media/title-compare.jpg") as im:
        assert im.width == 2 * 320 + 8
        left, right = im.getpixel((20, 150)), im.getpixel((340, 150))
    assert left[2] > left[0] and right[0] > right[2]  # blue before, red after


def test_gallery_markdown_and_insert(write_manifest, fake_cmd):
    p = cmd_manifest(write_manifest, fake_cmd)
    m = load(p)
    shoot(m)
    readme = p.parent / "README.md"
    readme.write_text("# Game\n\n<!-- darkroom:start -->\nold\n<!-- darkroom:end -->\n")
    md = gallery.markdown(m, readme)
    assert "![A blue title screen](docs/media/title.jpg)" in md
    assert "*The title screen.*" in md
    assert "![Walking.](docs/media/walk.gif)" in md and "[MP4 version](docs/media/walk.mp4)" in md
    assert gallery.insert(readme, md)
    assert "old" not in readme.read_text() and "title.jpg" in readme.read_text()
    sub = p.parent / "docs" / "MEDIA.md"
    assert "(media/title.jpg)" in gallery.markdown(m, sub)
    assert main(["gallery", str(p), "--format", "html"]) == 0
    assert "<figure>" in (p.parent / "docs/media/index.html").read_text()


def test_check_cli(write_manifest, fake_cmd, capsys):
    p = cmd_manifest(write_manifest, fake_cmd)
    assert main(["check", str(p)]) == 0
    bad = write_manifest({"shots": [{"name": "x", "kind": "movie"}]}, "bad.json")
    with pytest.raises(SystemExit) as e:
        main(["check", str(bad)])
    assert e.value.code == 2
    assert "kind: must be one of" in capsys.readouterr().err


def test_lock_is_exclusive(lock_dir):
    with lock("rig"):
        with pytest.raises(LockTimeout):
            with lock("rig", timeout=0.3):
                pass
    with lock("rig", timeout=0.3):  # free again
        pass
    assert (lock_dir / "rig.lock").exists()


def test_lock_serializes_command_shots(write_manifest, fake_cmd, tmp_path, monkeypatch):
    log = tmp_path / "runs.log"
    monkeypatch.setenv("DARKROOM_TEST_DISPLAY_LOG", str(log))
    def manifest(name):
        return load(write_manifest({"shots": [{"name": name, "backend": "command", "lock": "heavy",
                                               "device": {"width": 32, "height": 32},
                                               "argv": fake_cmd + ["{out_dir}", "8", "8", "--sleep", "0.6"]}]},
                                   f"{name}.json"))
    ts = [threading.Thread(target=shoot, args=(manifest(n),), kwargs={"log": lambda *_: None}) for n in "ab"]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    events = [l.split() for l in log.read_text().splitlines()]
    kinds = [e[-1] for e in sorted(events, key=lambda e: float(e[-2]))]
    assert kinds == ["start", "end", "start", "end"]


@pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb not installed")
def test_xvfb_gives_each_capture_its_own_display(write_manifest, fake_cmd, tmp_path, monkeypatch):
    log = tmp_path / "displays.log"
    monkeypatch.setenv("DARKROOM_TEST_DISPLAY_LOG", str(log))
    def manifest(name):
        return load(write_manifest({"shots": [{"name": name, "backend": "command", "xvfb": True,
                                               "device": {"width": 320, "height": 200},
                                               "argv": fake_cmd + ["{out_dir}", "8", "8", "--sleep", "0.5"]}]},
                                   f"{name}.json"))
    ts = [threading.Thread(target=shoot, args=(manifest(n),), kwargs={"log": lambda *_: None}) for n in "ab"]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    displays = {l.split()[0] for l in log.read_text().splitlines()}
    assert len(displays) == 2 and all(d.startswith(":") for d in displays)
