"""Command-shot options: the scratch cwd, `each`, `@list` splicing, `KEY+` defaults and `file`."""
import json
import os
import stat
import sys

from PIL import Image

from darkroom.cli import main
from darkroom.manifest import expand_argv, load, validate
from darkroom.shoot import shoot

WRITES_INI = ("import sys, pathlib; from PIL import Image; pathlib.Path('imgui.ini').write_text('x'); "
              "Image.new('RGB', (8, 8)).save(sys.argv[1] + '/shot.png')")


def quiet(*_):
    pass


def test_default_cwd_is_scratch_not_the_project(write_manifest):
    p = write_manifest({"shots": [{"name": "a", "backend": "command",
                                   "argv": [sys.executable, "-c", WRITES_INI, "{out_dir}"]}]})
    assert shoot(load(p), log=quiet) == 0
    assert not (p.parent / "imgui.ini").exists()
    assert not list(p.parent.rglob("imgui.ini"))
    meta = json.loads((p.parent / ".darkroom/takes/a/meta.json").read_text())
    assert meta["frames"] == ["00000.png"]  # only images from {out_dir}, never cwd clutter


def test_cwd_is_overridable(write_manifest):
    p = write_manifest({"shots": [{"name": "a", "backend": "command", "cwd": "{manifest_dir}",
                                   "argv": [sys.executable, "-c", WRITES_INI, "{out_dir}"]}]})
    assert shoot(load(p), log=quiet) == 0
    assert (p.parent / "imgui.ini").exists()


def test_relative_program_still_means_the_manifests(write_manifest, fake_cmd):
    script = write_manifest({}, "cap.py")  # just to get a path in tmp
    script.write_text(f"#!{sys.executable}\n" + (fake_cmd[1] and open(fake_cmd[1]).read()))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    p = write_manifest({"shots": [{"name": "a", "backend": "command",
                                   "argv": ["./cap.py", "{out_dir}", "16", "16"]}]})
    assert shoot(load(p), log=quiet) == 0
    assert (p.parent / "media/a.jpg").exists()


def test_each_runs_once_per_value(write_manifest, fake_cmd):
    p = write_manifest({"shots": [
        {"name": "season", "backend": "command", "device": {"width": 64, "height": 48},
         "each": ["200,40,40", "40,200,40", "40,40,200"],
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--color", "{value}"]},
        {"name": "orbit", "kind": "clip", "backend": "command", "device": {"width": 64, "height": 48},
         "each": ["9,9,9", "250,250,250"], "env": {"STEP": "{i}/{n}:{value}"},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--color", "{value}"], "gif": {"fps": 2}},
    ]})
    assert shoot(load(p), log=quiet) == 0
    out = p.parent / "media"
    # a still with `each` makes one still per value
    for name, channel in (("season-200-40-40.jpg", 0), ("season-40-200-40.jpg", 1), ("season-40-40-200.jpg", 2)):
        with Image.open(out / name) as im:
            px = im.getpixel((2, 2))
        assert max(range(3), key=lambda c: px[c]) == channel
    meta = json.loads((p.parent / ".darkroom/takes/orbit/meta.json").read_text())
    assert meta["runs"] == [0, 1] and meta["captions"] == ["9,9,9", "250,250,250"]
    with Image.open(out / "orbit.gif") as g:
        assert g.n_frames == 2


def test_each_values_in_file_template(write_manifest, fake_cmd):
    p = write_manifest({"shots": [{"name": "s", "backend": "command", "device": {"width": 32, "height": 32},
                                   "each": ["spring", "summer"], "file": "docs/{value}.png",
                                   "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]}]})
    assert shoot(load(p), log=quiet) == 0
    assert (p.parent / "docs/spring.png").exists() and (p.parent / "docs/summer.png").exists()


def test_each_validation():
    base = {"name": "a", "backend": "command", "argv": ["x"]}
    assert any("each: must be a non-empty list" in e for e in validate({"shots": [{**base, "each": []}]}))
    probs = validate({"shots": [{**base, "each": ["a", "b"], "repeat": 3}]})
    assert any("repeat: 'each' already runs 2 time(s)" in e for e in probs)
    assert validate({"shots": [{**base, "each": ["a", "b"], "repeat": 2}]}) == []
    assert validate({"shots": [{"name": "w", "url": "http://x/?s={value}", "each": ["a"]}]}) == []


def test_list_var_splices_into_argv(write_manifest, fake_cmd):
    v = {"common": ["--frames", "3"], "w": "16", "out_dir": "/o"}
    assert expand_argv(["g", "@common", "{w}", "@nope", "a@common"], v) == \
        ["g", "--frames", "3", "16", "@nope", "a@common"]
    p = write_manifest({"vars": {"common": ["--frames", "3"]}, "shots": [
        {"name": "c", "kind": "clip", "backend": "command", "device": {"width": 32, "height": 32},
         "argv": fake_cmd + ["{out_dir}", "@common", "{width}", "{height}"], "gif": {"fps": 4}}]})
    assert shoot(load(p), log=quiet) == 0
    assert len(json.loads((p.parent / ".darkroom/takes/c/meta.json").read_text())["frames"]) == 3
    # --var on a list var is split like a shell
    m = load(p, {"common": "--frames 5 --color '1,2,3'"})
    assert m.vars["common"] == ["--frames", "5", "--color", "1,2,3"]


def test_list_var_inside_a_string_is_pointed_out():
    probs = validate({"vars": {"common": ["--fast"]},
                      "shots": [{"name": "a", "backend": "command", "argv": ["g", "--x={common}"]}]})
    assert any("'{common}' is a list var" in e and '"@common"' in e for e in probs)
    assert any("vars.bad" in e for e in validate({"vars": {"bad": [{"x": 1}]}, "shots": [
        {"name": "a", "backend": "command", "argv": ["g"]}]}))


def test_defaults_argv_plus_appends(write_manifest, fake_cmd):
    data = {"defaults": {"backend": "command", "device": {"width": 32, "height": 32},
                         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]},
            "shots": [{"name": "one"},
                      {"name": "three", "kind": "clip", "argv+": ["--frames", "3"], "gif": {"fps": 4}},
                      {"name": "web", "url": "http://x/", "backend": "web"}]}
    p = write_manifest(data)
    m = load(p)
    one, three, web = m.shots
    assert three["argv"] == fake_cmd + ["{out_dir}", "{width}", "{height}", "--frames", "3"]
    assert "argv" not in web.raw  # the command default is dropped for a web shot
    assert shoot(m, only=["one", "three"], log=quiet) == 0
    assert len(json.loads((p.parent / ".darkroom/takes/three/meta.json").read_text())["frames"]) == 3
    probs = validate({**data, "shots": [{"name": "x", "argv": ["a"], "argv+": ["b"]}]})
    assert any("has both 'argv' and 'argv+'" in e for e in probs)
    assert any("only argv+" in e for e in validate({**data, "shots": [{"name": "x", "caption+": ["b"]}]}))


def test_file_puts_the_output_in_place(write_manifest, fake_cmd):
    p = write_manifest({"shots": [
        {"name": "title", "backend": "command", "device": {"width": 40, "height": 30},
         "file": "docs/images/seasons.png", "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]},
        {"name": "walk", "kind": "clip", "backend": "command", "device": {"width": 40, "height": 30},
         "file": "docs/img/walk.gif", "poster": 0, "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--frames", "3"]},
    ]})
    m = load(p)
    assert shoot(m, log=quiet) == 0
    with Image.open(p.parent / "docs/images/seasons.png") as im:
        assert im.format == "PNG"
    assert (p.parent / "docs/img/walk.gif").exists() and (p.parent / "docs/img/walk-poster.jpg").exists()
    assert not (p.parent / "media").exists()
    assert shoot(m, only=["title"], tag="before", log=quiet) == 0
    assert (p.parent / "docs/images/seasons.before.png").exists()


def test_file_validation():
    base = {"name": "a", "backend": "command", "argv": ["x"]}
    assert any("extension picks the format" in e for e in validate({"shots": [{**base, "file": "a.gif"}]}))
    assert any("disagrees with file" in e for e in validate({"shots": [{**base, "file": "a.png", "format": "jpg"}]}))
    assert any("relative to the manifest" in e for e in validate({"shots": [{**base, "file": "/abs/a.png"}]}))
    assert validate({"shots": [{**base, "kind": "clip", "file": "docs/a.mp4"}]}) == []


def test_cwd_must_be_a_string():
    probs = validate({"shots": [{"name": "a", "backend": "command", "argv": ["x"], "cwd": ["a"]}]})
    assert any("cwd: must be a path" in e for e in probs)
