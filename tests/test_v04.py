"""0.4: prep shots, inputs and depends (#10), named command outputs (#11), check warnings
(#12), and the web backend's initScript and context (#6)."""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from darkroom import gallery
from darkroom.backends.web import init_scripts
from darkroom.cli import main
from darkroom.manifest import Manifest, lint, load, validate
from darkroom.shoot import plan, shoot

PY = sys.executable

# Writes data/stats.json (a count of its runs in data/runs.txt), like a heavy rig run would.
PREP_SCRIPT = (
    "import pathlib, sys; d = pathlib.Path('data'); d.mkdir(exist_ok=True); "
    "r = d / 'runs.txt'; n = int(r.read_text()) + 1 if r.exists() else 1; r.write_text(str(n)); "
    "(d / 'stats.json').write_text('{\"value\": %s}' % sys.argv[1])"
)
# Reads data/stats.json and draws an image whose red channel is the value.
RENDER_SCRIPT = (
    "import json, sys, pathlib; from PIL import Image; "
    "v = json.loads(pathlib.Path(sys.argv[2], 'data/stats.json').read_text())['value']; "
    "Image.new('RGB', (64, 48), (v, 10, 10)).save(sys.argv[1] + '/chart.png')"
)
# Writes two views (top.png, oblique.png) of one run, and counts the runs.
VIEWS_SCRIPT = (
    "import sys, pathlib; from PIL import Image; out = pathlib.Path(sys.argv[1]); "
    "c = pathlib.Path(sys.argv[2]) / 'view-runs.txt'; c.write_text(str(int(c.read_text()) + 1 if c.exists() else 1)); "
    "Image.new('RGB', (64, 48), (200, 20, 20)).save(out / 'top.png'); "
    "Image.new('RGB', (64, 48), (20, 20, 200)).save(out / 'oblique.png'); "
    "Image.new('RGB', (8, 8)).save(out / 'debug.png')"
)


def quiet(*_):
    pass


def prep_manifest(write_manifest, value="40"):
    return write_manifest({"vars": {"value": value}, "shots": [
        {"name": "chart", "backend": "command", "depends": ["rig"], "device": {"width": 64, "height": 48},
         "argv": [PY, "-c", RENDER_SCRIPT, "{out_dir}", "{manifest_dir}"], "format": "png"},
        {"name": "rig", "kind": "prep", "argv": [PY, "-c", PREP_SCRIPT, "{value}"],
         "outputs": ["data/stats.json"], "lock": "rig"},
    ]})


def runs(p: Path, name="data/runs.txt") -> int:
    f = p.parent / name
    return int(f.read_text()) if f.exists() else 0


# ------------------------------------------------------------------ #10 prep, inputs, depends

def test_prep_runs_first_and_skips_when_up_to_date(write_manifest):
    p = prep_manifest(write_manifest)
    m = load(p)
    assert [s.name for s in plan(m)] == ["rig", "chart"]  # depends beats manifest order
    logs = []
    assert shoot(m, log=logs.append) == 0
    assert runs(p) == 1
    with Image.open(p.parent / "media/chart.png") as im:
        assert im.getpixel((5, 5))[0] == 40
    assert not (p.parent / "media/rig").exists()  # a prep makes no media
    logs.clear()
    assert shoot(load(p), log=logs.append) == 0
    assert runs(p) == 1
    assert any(l.startswith("= rig: up to date") for l in logs)
    assert any(l.startswith("= chart: up to date") for l in logs)


def test_prep_reruns_on_change_and_dependents_reshoot(write_manifest):
    p = prep_manifest(write_manifest)
    shoot(load(p), log=quiet)
    # A var the prep reads changes: the prep reruns, and the shot that reads its output reshoots
    # (a prep's outputs feed the capture hash of the shots that depend on it).
    logs = []
    assert shoot(load(p, {"value": "90"}), log=logs.append) == 0
    assert runs(p) == 2
    assert any(l.startswith("> chart") for l in logs)
    with Image.open(p.parent / "media/chart.png") as im:
        assert im.getpixel((5, 5))[0] == 90
    # A deleted output reruns the prep.
    (p.parent / "data/stats.json").unlink()
    assert shoot(load(p, {"value": "90"}), log=quiet) == 0
    assert runs(p) == 3


def test_only_pulls_in_the_prep_unless_up_to_date(write_manifest):
    p = prep_manifest(write_manifest)
    logs = []
    assert shoot(load(p), only=["chart"], log=logs.append) == 0
    assert runs(p) == 1 and logs[0].startswith("> rig (prep)")
    logs.clear()
    assert shoot(load(p), only=["chart"], force=True, log=logs.append) == 0
    assert runs(p) == 1  # --force reshoots chart, not the pulled-in prep
    assert any(l.startswith("> chart") for l in logs)
    assert shoot(load(p), only=["rig"], force=True, log=quiet) == 0
    assert runs(p) == 2


def test_failed_prep_skips_its_dependents(write_manifest):
    p = write_manifest({"shots": [
        {"name": "rig", "kind": "prep", "argv": [PY, "-c", "pass"], "outputs": ["never.json"], "retries": 0},
        {"name": "chart", "backend": "command", "depends": ["rig"], "argv": [PY, "-c", "pass"]},
    ]})
    logs = []
    assert shoot(load(p), log=logs.append) == 2
    assert any("the prep did not write never.json" in l for l in logs)
    assert "! chart: skipped: depends on 'rig', which failed" in logs


def test_inputs_reshoot_when_their_content_changes(write_manifest, fake_cmd):
    p = write_manifest({"shots": [
        {"name": "card", "backend": "command", "device": {"width": 32, "height": 32},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"], "inputs": ["scenes/*.json", "notes.txt"]},
    ]})
    (p.parent / "scenes").mkdir()
    (p.parent / "scenes/a.json").write_text("{}")
    assert shoot(load(p), log=quiet) == 0
    logs = []
    shoot(load(p), log=logs.append)
    assert logs[0].startswith("= card: up to date")
    for change in (lambda: (p.parent / "scenes/a.json").write_text('{"x": 1}'),
                   lambda: (p.parent / "scenes/b.json").write_text("{}"),      # a new glob match
                   lambda: (p.parent / "notes.txt").write_text("now exists")):  # a missing input appears
        change()
        logs.clear()
        shoot(load(p), log=logs.append)
        assert logs[0].startswith("> card"), logs
    # Touching a file without changing it is not a change.
    (p.parent / "notes.txt").write_text("now exists")
    logs.clear()
    shoot(load(p), log=logs.append)
    assert logs[0].startswith("= card: up to date")


def test_hashes_of_03_shots_are_unchanged():
    # Values from darkroom 0.3.0 for the same manifest: 0.4 must not reshoot unchanged shots.
    data = {"vars": {"color": "1,2,3"}, "defaults": {"device": "phone"}, "shots": [
        {"name": "a", "backend": "command", "argv": ["prog", "{out_dir}", "--c", "{color}"], "caption": "x"},
        {"name": "b", "kind": "clip", "url": "http://localhost/{color}", "steps": [{"frames": 3}], "gif": {"fps": 8}},
        {"name": "c", "kind": "pair", "before": "a.png", "after": "b.png"}]}
    m = Manifest(Path("/nowhere/media.json"), data, dict(data["vars"]))
    got = {s.name: (m.capture_hash(s, None), m.encode_hash(s, "t")) for s in m.shots}
    assert got == {"a": ("547d51e308ba1cd7", "fd23c50bdf44422f"), "b": ("f7438a3c3024143b", "0cdf000eb2df3c27"),
                   "c": ("d998a2da7897d1c6", "f19a931426c8f0ff")}


def test_prep_and_depends_validation():
    def problems(*shots, defaults=None):
        return validate({"shots": list(shots), **({"defaults": defaults} if defaults else {})})
    assert any("outputs: required" in x for x in problems({"name": "r", "kind": "prep", "argv": ["x"]}))
    pp = problems({"name": "r", "kind": "prep", "argv": ["x"], "outputs": ["a.json"], "gif": {}, "each": [1]})
    assert any(".gif: not for a prep (a prep makes no images)" in x for x in pp)
    assert any(".each: not for a prep (it runs once" in x for x in pp)
    assert any("'out/*.json' is a glob" in x
               for x in problems({"name": "r", "kind": "prep", "argv": ["x"], "outputs": ["out/*.json"]}))
    assert any("a prep runs a command" in x
               for x in problems({"name": "r", "kind": "prep", "backend": "web", "argv": ["x"], "outputs": ["a"]}))
    # Defaults for image shots don't leak into a prep.
    assert problems({"name": "r", "kind": "prep", "argv": ["x"], "outputs": ["a"]},
                    {"name": "s", "url": "http://x"}, defaults={"format": "png", "caption": "c", "url": "http://x"}) == []
    assert any("depends[0]: no shot named 'rgi' (did you mean 'rig'?)" in x
               for x in problems({"name": "rig", "kind": "prep", "argv": ["x"], "outputs": ["a"]},
                                 {"name": "s", "url": "http://x", "depends": ["rgi"]}))
    assert any("cycle a -> b -> a" in x
               for x in problems({"name": "a", "url": "http://x", "depends": ["b"]},
                                 {"name": "b", "url": "http://x", "depends": ["a"]}))
    assert any("cannot depend on itself" in x for x in problems({"name": "a", "url": "http://x", "depends": ["a"]}))
    assert any("inputs: must be a list of paths or globs" in x for x in problems({"name": "a", "url": "http://x", "inputs": "x"}))
    assert any("'r' is a prep, which makes no images" in x
               for x in problems({"name": "r", "kind": "prep", "argv": ["x"], "outputs": ["a"]},
                                 {"name": "p", "kind": "pair", "of": "r"}))


# ------------------------------------------------------------------ #11 named outputs

def views_manifest(write_manifest, **extra):
    shot = {"name": "map", "backend": "command", "device": {"width": 64, "height": 48},
            "argv": [PY, "-c", VIEWS_SCRIPT, "{out_dir}", "{manifest_dir}"], "caption": "The map.",
            "outputs": {"top": {"file": "top.png", "caption": "From above.", "alt": "A red map from above"},
                        "oblique": "oblique.png"}}
    shot.update(extra)
    return write_manifest({"out": "docs/media", "shots": [shot]})


def test_named_outputs_from_one_run(write_manifest):
    p = views_manifest(write_manifest)
    assert shoot(load(p), log=quiet) == 0
    out = p.parent / "docs/media"
    assert runs(p, "view-runs.txt") == 1
    with Image.open(out / "map-top.jpg") as a, Image.open(out / "map-oblique.jpg") as b:
        assert a.getpixel((5, 5))[0] > 150 and b.getpixel((5, 5))[2] > 150
    assert not (out / "map.jpg").exists() and not list(out.glob("*debug*"))
    md = gallery.markdown(load(p))
    assert "![A red map from above](docs/media/map-top.jpg)" in md
    assert "*From above.*" in md
    assert "![The map. (oblique)](docs/media/map-oblique.jpg)" in md
    assert md.index("map-top") < md.index("map-oblique") < md.index("*The map.*")
    page = gallery.html_page(load(p), p.parent / "docs/media/index.html")
    assert page.count("<figure>") == 2 and 'alt="A red map from above"' in page
    # Captions and alt are presentation: editing them never reshoots.
    data = json.loads(p.read_text())
    data["shots"][0]["outputs"]["top"]["caption"] = "Seen from the top."
    p.write_text(json.dumps(data))
    logs = []
    shoot(load(p), log=logs.append)
    assert logs[0].startswith("= map: up to date")
    (p.parent / "docs/media/map-top.jpg").unlink()  # a missing output does not shift the others
    assert "![The map. (oblique)](docs/media/map-oblique.jpg)" in gallery.markdown(load(p))


def test_named_outputs_file_template_and_missing_file(write_manifest):
    p = views_manifest(write_manifest, file="docs/views/{key}.png")
    assert shoot(load(p), log=quiet) == 0
    assert (p.parent / "docs/views/top.png").exists() and (p.parent / "docs/views/oblique.png").exists()
    assert {x.name for x in gallery._outputs(load(p), gallery.State(load(p)), load(p).shot("map"))} == {"top.png", "oblique.png"}
    p = views_manifest(write_manifest, outputs={"top": "top.png", "side": "side.png"}, retries=0)
    logs = []
    assert shoot(load(p), log=logs.append) == 1
    assert any("did not write outputs.side: 'side.png' into {out_dir} (it wrote: debug.png, oblique.png, top.png)"
               in l for l in logs), logs


def test_named_outputs_validation():
    def problems(**shot):
        return validate({"shots": [{"name": "s", **shot}]})
    cmd = {"backend": "command", "argv": ["x"]}
    assert any("only a command still has named outputs" in x
               for x in problems(kind="clip", outputs={"a": "a.png"}, **cmd))
    assert any("a web still names its images" in x for x in problems(url="http://x", outputs={"a": "a.png"}))
    assert any("'each' and named outputs don't mix" in x for x in problems(outputs={"a": "a.png"}, each=[1, 2], **cmd))
    assert any("outputs.a: 'a.txt' is not an image" in x for x in problems(outputs={"a": "a.txt"}, **cmd))
    assert any("outputs.a: unknown key 'captoin' (did you mean 'caption'?)" in x
               for x in problems(outputs={"a": {"file": "a.png", "captoin": "x"}}, **cmd))
    assert any("outputs.a.file: required" in x for x in problems(outputs={"a": {"caption": "x"}}, **cmd))
    assert problems(outputs={"a": "a.png"}, file="docs/{key}.jpg", **cmd) == []
    assert any("unknown variable '{key}'" in x for x in problems(file="docs/{key}.jpg", **cmd))


# ------------------------------------------------------------------ #12 check warnings

def test_check_warns_about_missing_paths(write_manifest, capsys):
    p = write_manifest({"vars": {"script": "tools/missing.py"}, "shots": [
        {"name": "a", "backend": "command", "argv": ["no-such-program-xyz", "render.py", "--out", "{out_dir}"]},
        {"name": "b", "backend": "command", "argv": [PY, "{script}", "scenes/one.json", "{value}", "1.5", "hello"],
         "each": ["x"]},
        {"name": "c", "backend": "command", "argv": ["./build/game", "data/stats.json"], "inputs": ["levels/*.txt"]},
        {"name": "rig", "kind": "prep", "argv": [PY, "tools/rig.py"], "outputs": ["data/stats.json"]},
        {"name": "w", "url": "http://x", "initScript": [{"file": "init.js"}, "window.x = 1"]},
    ]})
    (p.parent / "tools").mkdir()
    (p.parent / "tools/rig.py").write_text("")
    assert main(["check", str(p)]) == 0
    err = capsys.readouterr().err
    assert "shots[0] (a).argv[0]: program 'no-such-program-xyz' is not on PATH" in err
    assert "shots[0] (a).argv[1]: 'render.py' does not exist (relative to the manifest's folder)" in err
    assert "shots[1] (b).argv[1]: 'tools/missing.py' does not exist" in err
    assert "shots[1] (b).argv[2]: 'scenes/one.json' does not exist" in err
    assert "shots[2] (c).argv[0]: './build/game' does not exist" in err
    assert "shots[2] (c).inputs[0]: 'levels/*.txt' matches no files (yet)" in err
    assert "shots[4] (w).initScript: file 'init.js' does not exist" in err
    assert "data/stats.json" not in err  # a prep makes it
    assert "rig" not in err and "hello" not in err and "1.5" not in err and "{value}" not in err
    assert err.count("warning:") == 7


# ------------------------------------------------------------------ #6 initScript and context

def test_init_and_context_validation_and_hash(tmp_path):
    def problems(**shot):
        return validate({"shots": [{"name": "s", "url": "http://x", **shot}]})
    assert problems(initScript="localStorage.x = 1", context={"reducedMotion": "reduce", "locale": "de-DE"}) == []
    assert problems(initScript=[{"file": "a.js"}, "x()"]) == []
    assert any("initScript[1]: must be JS source (a string) or {\"file\"" in x for x in problems(initScript=["x", 3]))
    assert any("initScript: must be JS source" in x for x in problems(initScript={"path": "a.js"}))
    assert any("initScript.file: must be relative" in x for x in problems(initScript={"file": "/abs/a.js"}))
    assert any("context: must be an object of Playwright browser.newContext options" in x for x in problems(context=["x"]))
    assert any("context.viewport: set by the shot's device; set it there instead" in x
               for x in problems(context={"viewport": {"width": 1}}))
    assert any("context.userAgent: set by the shot's userAgent" in x for x in problems(context={"userAgent": "x"}))
    assert any("unknown key 'initScript'" in x
               for x in validate({"shots": [{"name": "s", "backend": "command", "argv": ["x"], "initScript": "x"}]}))
    # The init file's content feeds the capture hash.
    (tmp_path / "init.js").write_text("window.a = 1")
    p = tmp_path / "media.json"
    p.write_text(json.dumps({"shots": [{"name": "s", "url": "http://x", "initScript": {"file": "init.js"}}]}))
    m = load(p)
    h = m.capture_hash(m.shot("s"), None)
    (tmp_path / "init.js").write_text("window.a = 2")
    assert load(p).capture_hash(m.shot("s"), None) != h


def test_init_scripts_job(tmp_path):
    (tmp_path / "init.js").write_text("const keep = {value};")
    got = init_scripts([{"file": "{dir}/init.js"}, "localStorage.q = '{q}'"], {"dir": ".", "q": "high"}, tmp_path)
    assert got == ["const keep = {value};", "localStorage.q = 'high'"]  # files are sent as they are
    with pytest.raises(Exception, match="initScript: cannot read 'nope.js'"):
        init_scripts({"file": "nope.js"}, {}, tmp_path)
