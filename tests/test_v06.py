"""0.6: the full console log (#14), per-shot vars and loops over devices (#15), new web steps
(#16), page sessions (#17), the gallery's per-device table (#18) and ``shoot --jobs``."""
import json
import sys
from pathlib import Path

from PIL import Image

from darkroom import gallery, procs
from darkroom.cli import main
from darkroom.manifest import Manifest, load, validate
from darkroom.shoot import _console_failures, console_types, plan, shoot, strip_device, units

PY = sys.executable

# Writes one PNG of {width}x{height}, and logs "<label> start|end <time>" around a nap, so a
# test can see which captures overlapped.
TIMED = (
    "import sys, time, pathlib; from PIL import Image; out, w, h, label, nap, log = sys.argv[1:7]; "
    "f = open(log, 'a'); f.write('%s start %.3f\\n' % (label, time.time())); f.flush(); time.sleep(float(nap)); "
    "Image.new('RGB', (int(w), int(h)), (90, 30, 30)).save(pathlib.Path(out) / 'f.png'); "
    "f.write('%s end %.3f\\n' % (label, time.time())); f.close()"
)


def quiet(*_):
    pass


def problems(*shots, **top):
    return validate({"shots": list(shots), **top})


# ------------------------------------------------------------------ compatibility

def test_hashes_of_05_shots_are_unchanged():
    # Values from darkroom 0.5.0 for the same manifest: 0.6 must not reshoot unchanged shots.
    data = {"vars": {"color": "1,2,3", "base": "http://localhost/"},
            "devices": {"tablet": {"width": 820, "height": 1180, "dpr": 2, "touch": True}},
            "defaults": {"device": "phone"}, "shots": [
        {"name": "a", "backend": "command", "argv": ["prog", "{out_dir}", "--c", "{color}"], "caption": "x"},
        {"name": "b", "kind": "clip", "url": "{base}{color}", "steps": [{"frames": 3}], "gif": {"fps": 8}},
        {"name": "c", "kind": "pair", "before": "a.png", "after": "b.png"},
        {"name": "d", "url": "{base}", "device": "tablet", "failOnConsoleError": True, "file": "docs/{name}.png",
         "steps": [{"tap": "#go"}, {"capture": "menu"}, {"click": "#x"}, {"capture": "map"}]},
        {"name": "e", "url": "{base}?s={value}", "each": ["spring", "autumn"], "initScript": "window.x=1",
         "context": {"locale": "de-DE"}},
        {"name": "f", "backend": "command", "argv": ["prog", "{out_dir}"], "outputs": {"top": "top.png"},
         "device": "desktop"}]}
    m = Manifest(Path("/nowhere/media.json"), data, dict(data["vars"]))
    got = {s.name: (m.capture_hash(s, None), m.encode_hash(s, "t")) for s in m.shots}
    assert got == {
        "a": ("84d50eca13a897ab", "fd23c50bdf44422f"), "b": ("d12d976395fccdf2", "0cdf000eb2df3c27"),
        "c": ("60c68d3ad75752a0", "f19a931426c8f0ff"), "d": ("5b2379a88a832cef", "621bfb379e0e2bed"),
        "e": ("4ac519bef02d712b", "fd23c50bdf44422f"), "f": ("f845d3ab25ad660d", "fd23c50bdf44422f")}


# ------------------------------------------------------------------ #15 vars and devices

def test_shot_vars_merge_over_manifest_vars_and_var_still_wins(write_manifest, fake_cmd):
    p = write_manifest({"vars": {"color": "10,10,10", "size": "32"}, "shots": [
        {"name": "red", "backend": "command", "vars": {"color": "200,{size},10"},
         "device": {"width": 32, "height": 32}, "format": "png",
         "argv": fake_cmd + ["{out_dir}", "{size}", "{size}", "--color", "{color}"]},
        {"name": "plain", "backend": "command", "device": {"width": 32, "height": 32}, "format": "png",
         "argv": fake_cmd + ["{out_dir}", "{size}", "{size}", "--color", "{color}"]}]})
    m = load(p)
    red, plain = m.shots
    assert m.shot_vars(red)["color"] == "200,32,10" and m.shot_vars(plain) is m.vars
    assert shoot(m, log=quiet) == 0
    with Image.open(p.parent / "media/red.png") as im:
        assert im.getpixel((0, 0)) == (200, 32, 10)
    # --var wins over the shot's own vars, and changes only the shots that use it.
    m2 = load(p, {"color": "1,2,3"})
    assert m2.shot_vars(m2.shots[0])["color"] == "1,2,3"
    m3 = load(p, {"size": "40"})
    assert m3.capture_hash(m3.shots[0], None) != m.capture_hash(m.shots[0], None)


def test_each_over_devices_and_the_table_gallery(write_manifest, fake_cmd):
    p = write_manifest({
        "gallery": {"layout": "table", "by": "device", "widths": {"phone": 120}},
        "devices": {"tablet": {"width": 80, "height": 100}, "small": {"width": 40, "height": 80}},
        "shots": [
            {"name": "home", "backend": "command", "each": ["small", "tablet"], "device": "{value}",
             "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"], "alt": "Home"},
            {"name": "map-small", "backend": "command", "device": "small", "gallery": {"row": "map"},
             "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]},
            {"name": "walk", "kind": "clip", "backend": "command", "device": {"width": 32, "height": 32},
             "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--frames", "3"]}]})
    m = load(p)
    assert m.device_varies(m.shots[0])
    assert shoot(m, log=quiet) == 0
    with Image.open(p.parent / "media/home-small.jpg") as a, Image.open(p.parent / "media/home-tablet.jpg") as b:
        assert a.size == (40, 80) and b.size == (80, 100)
    meta = json.loads((p.parent / ".darkroom/takes/home/meta.json").read_text())
    assert meta["devices"] == ["small", "tablet"]
    state = json.loads((p.parent / ".darkroom/state.json").read_text())
    assert [(c["row"], c["device"]) for c in state["home"]["cells"]] == [("home", "small"), ("home", "tablet")]
    md = gallery.markdown(m, p.parent / "README.md", layout=gallery.Layout.of(m))
    assert md.startswith("<table>\n<tr><th></th><th>small</th><th>tablet</th></tr>")
    assert '<tr><td><b>home</b></td><td><img src="media/home-small.jpg" alt="Home (home, small)"' in md
    assert '<tr><td><b>map</b></td><td><img src="media/map-small.jpg"' in md and "<td></td></tr>" in md
    assert "![walk](media/walk.gif)" in md  # a clip stays in the list below the table
    # A column's width: equal heights by default (small is half as wide as tall: 0.5 / (0.5 + 0.8))
    assert 'width="338"' in md
    # --layout list overrides the manifest's default
    assert "<table>" not in gallery.markdown(m, p.parent / "README.md", layout=gallery.Layout.of(m, "list"))
    page = gallery.html_page(m, p.parent / "media/index.html", layout=gallery.Layout.of(m))
    assert '<table class=devices>' in page and 'style="width:338px"' in page
    # The CLI flags
    doc = p.parent / "PR.md"
    doc.write_text("<!-- darkroom:shot home /-->\n")
    assert main(["gallery", str(p), "--insert", str(doc), "--layout", "table", "--by", "device"]) == 0
    assert "<th>small</th><th>tablet</th>" in doc.read_text()


def test_table_cells_from_older_state(write_manifest, fake_cmd):
    """Outputs shot before 0.6 (no cells in the state) still make a table for fixed devices."""
    p = write_manifest({"shots": [
        {"name": "title-phone", "backend": "command", "device": "phone",
         "argv": fake_cmd + ["{out_dir}", "39", "84"]},
        {"name": "title-desktop", "backend": "command", "device": "desktop",
         "argv": fake_cmd + ["{out_dir}", "64", "36"]}]})
    m = load(p)
    assert shoot(m, log=quiet) == 0
    sf = p.parent / ".darkroom/state.json"
    st = json.loads(sf.read_text())
    for v in st.values():
        v.pop("cells")
    sf.write_text(json.dumps(st))
    md = gallery.markdown(m, p.parent / "README.md", layout=gallery.Layout("table"))
    assert "<th>phone</th><th>desktop</th>" in md
    assert md.count("<tr><td><b>title</b></td>") == 1  # one row: the device is not part of the screen


def test_strip_device():
    assert strip_device("title-phone", "phone") == "title"
    assert strip_device("phone.map", "phone") == "map"
    assert strip_device("telephone-x", "phone") == "telephone-x"
    assert strip_device("phone", "phone") == ""


def test_vars_devices_and_capture_name_validation():
    web = {"name": "s", "url": "http://x/"}
    pp = problems(dict(web, each=["phone", "watch"], device="{value}"))
    assert any("device: '{value}' gives 'watch' for each value 'watch', which is not a device" in x for x in pp)
    assert any("device: '{value}' uses {value}, which only 'each' sets" in x for x in problems(dict(web, device="{value}")))
    assert any("device: unknown variable '{dvc}'" in x for x in problems(dict(web, vars={"dev": "phone"}, device="{dvc}")))
    assert problems(dict(web, vars={"dev": "phone"}, device="{dev}")) == []
    assert any("vars.value: 'value' is set by darkroom itself" in x for x in problems(dict(web, vars={"value": 1})))
    assert any("vars.k: must be a string" in x for x in problems(dict(web, vars={"k": {"a": 1}})))
    # each + named captures: every name needs {value} (or {i})
    pp = problems(dict(web, each=["a", "b"], steps=[{"capture": "menu"}]))
    assert any("capture name 'menu' would be written once per value" in x and "'menu-{value}'" in x for x in pp)
    assert problems(dict(web, each=["a", "b"], steps=[{"capture": "menu-{value}"}])) == []
    assert any("capture name 'm-{vlue}': unknown variable '{vlue}'" in x
               for x in problems(dict(web, steps=[{"capture": "m-{vlue}"}])))
    # A shot var is known to the url and argv templates
    assert problems(dict(web, url="http://x/{page}", vars={"page": "a"})) == []
    assert any("unknown variable '{page}'" in x for x in problems(dict(web, url="http://x/{page}")))


def test_top_level_gallery_validation():
    s = {"name": "s", "url": "http://x/"}
    assert any("gallery.layout: must be one of list, table" in x for x in problems(s, gallery={"layout": "grid"}))
    assert any("gallery.by: must be 'device'" in x for x in problems(s, gallery={"layout": "table", "by": "tag"}))
    assert any("gallery.by: only for" in x for x in problems(s, gallery={"by": "device"}))
    assert any("gallery.widths: must be an object" in x for x in problems(s, gallery={"layout": "table", "widths": [1]}))
    assert problems(dict(s, gallery={"row": "title"}), gallery={"layout": "table", "widths": {"phone": 200}}) == []


# ------------------------------------------------------------------ #14 console, #16 steps

def test_console_options():
    from darkroom.manifest import Shot
    assert console_types(Shot({"failOnConsoleError": True}, 0)) == {"error"}
    assert console_types(Shot({"failOnConsole": ["warning"], "failOnConsoleError": True}, 0)) == {"error", "warning"}
    log = [{"type": "warning", "text": "w"}, {"type": "pageerror", "text": "boom"}, {"type": "log", "text": "x"}]
    assert _console_failures(log, ["boom"], {"error", "warning"}) == {"warning": 1, "error": 1}
    assert _console_failures(log, ["boom"], set()) == {}
    pp = problems({"name": "s", "url": "u", "failOnConsole": ["eror"]})
    assert any("failOnConsole: unknown console type 'eror' (did you mean 'error'?)" in x for x in pp)
    assert any("failOnConsole: must be a list" in x for x in problems({"name": "s", "url": "u", "failOnConsole": "error"}))


def test_step_validation_0_6():
    def steps(*st):
        return problems({"name": "s", "url": "u", "steps": list(st)})
    assert steps({"stable": "#hint", "timeout": 5000}, {"tap": {"at": [0.5, 0.25], "of": "canvas"}},
                 {"click": {"at": [0, 1], "of": "#b"}, "force": True}, {"tap": "#b", "force": False}) == []
    assert any("tap.at: must be [fx, fy], fractions (0 to 1)" in x for x in steps({"tap": {"at": [50, 50], "of": "c"}}))
    assert any("tap.of: required" in x for x in steps({"tap": {"at": [0.5, 0.5]}}))
    assert any("tap: unknown key 'off'" in x for x in steps({"tap": {"at": [0.5, 0.5], "of": "c", "off": 1}}))
    assert any("force: must be true or false" in x for x in steps({"click": "#b", "force": 1}))
    assert any("stable: must be a string (a CSS selector" in x for x in steps({"stable": 3}))
    assert any("timeout: must be a positive number" in x for x in steps({"stable": "#a", "timeout": -1}))
    assert any("unknown key 'force'" in x for x in steps({"js": "1", "force": True}))


# ------------------------------------------------------------------ #17 sessions

def test_session_units_and_validation():
    data = {"defaults": {"url": "http://x/", "session": "game", "device": "phone"}, "shots": [
        {"name": "a"}, {"name": "b"}, {"name": "solo", "session": "other"}, {"name": "c"},
        {"name": "cmd", "backend": "command", "argv": ["x"], "session": "game"}]}
    assert any("cmd).session: only web shots share a page" in x for x in validate(data))
    data["shots"].pop()
    m = Manifest(Path("/nowhere/media.json"), data, {})
    assert [[s.name for s in u] for u in units(plan(m))] == [["a", "b"], ["solo"], ["c"]]
    pp = validate({"defaults": {"session": "g"}, "shots": [
        {"name": "a", "url": "http://x/"}, {"name": "b", "url": "http://y/"},
        {"name": "c", "url": "http://x/", "device": "desktop", "setup": [{"wait": 1}]}]})
    assert any("(b).url: differs from 'a', the first shot of session 'g'" in x for x in pp)
    assert any("(c).setup: differs from 'a'" in x for x in pp)
    assert any("session: 'each' opens a fresh page per value" in x
               for x in problems({"name": "a", "url": "u", "session": "g", "each": [1, 2]}))
    assert any("session: must be a name" in x for x in problems({"name": "a", "url": "u", "session": "a b"}))
    # session is not in the capture hash: adding one never reshoots
    m = Manifest(Path("/nowhere/media.json"), {"shots": [{"name": "a", "url": "u"}]}, {})
    m2 = Manifest(Path("/nowhere/media.json"), {"shots": [{"name": "a", "url": "u", "session": "g"}]}, {})
    assert m.capture_hash(m.shots[0], None) == m2.capture_hash(m2.shots[0], None)


# ------------------------------------------------------------------ shoot --jobs

def intervals(log: Path) -> dict[str, tuple[float, float]]:
    out: dict[str, list[float]] = {}
    for line in log.read_text().splitlines():
        label, what, t = line.split()
        out.setdefault(label, [0.0, 0.0])[0 if what == "start" else 1] = float(t)
    return {k: (a, b) for k, (a, b) in out.items()}


def overlap(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def test_jobs_run_shots_at_once_but_respect_locks_and_depends(write_manifest, tmp_path):
    log = tmp_path / "times.log"

    def shot(name, nap, **kw):
        return {"name": name, "backend": "command", "device": {"width": 16, "height": 16}, "retries": 0,
                "argv": [PY, "-c", TIMED, "{out_dir}", "16", "16", name, str(nap), str(log)], **kw}
    p = write_manifest({"shots": [
        shot("a", 1.0), shot("b", 1.0),
        shot("c", 0.6, lock="heavy"), shot("d", 0.6, lock="heavy"),
        shot("e", 0.2, depends=["a"]),
        {"name": "grid", "kind": "grid", "tiles": ["a", "b"]}]})
    logs = []
    assert main(["shoot", str(p), "--jobs", "4"]) == 0
    iv = intervals(log)
    assert overlap(iv["a"], iv["b"])                      # independent shots ran at once
    assert not overlap(iv["c"], iv["d"])                  # one lock: never together
    assert iv["e"][0] >= iv["a"][1]                       # a dependent waits
    assert (p.parent / "media/grid.jpg").exists()          # composites after everything
    # a log prefix per shot
    log.unlink()
    assert shoot(load(p), force=True, jobs=3, log=logs.append) == 0
    assert any(l.startswith("[a] > a (still, command)") for l in logs), logs
    assert any(l.startswith("[grid] > grid (grid)") for l in logs)


def test_jobs_failure_skips_dependents(write_manifest, tmp_path):
    p = write_manifest({"shots": [
        {"name": "bad", "backend": "command", "retries": 0, "argv": [PY, "-c", "raise SystemExit(3)"]},
        {"name": "after", "backend": "command", "depends": ["bad"], "argv": [PY, "-c", "pass"]},
        {"name": "fine", "backend": "command", "device": {"width": 16, "height": 16},
         "argv": [PY, "-c", TIMED, "{out_dir}", "16", "16", "fine", "0", str(tmp_path / "t.log")]}]})
    logs = []
    assert shoot(load(p), jobs=2, log=logs.append) == 2
    assert any("[after] ! after: skipped: depends on 'bad', which failed" in l for l in logs), logs
    assert (p.parent / "media/fine.jpg").exists()


def test_jobs_validation(capsys, write_manifest):
    p = write_manifest({"shots": [{"name": "a", "url": "u"}]})
    import pytest
    with pytest.raises(SystemExit):
        main(["shoot", str(p), "--jobs", "0"])
    assert "must be a whole number of 1 or more" in capsys.readouterr().err


def test_run_registry_holds_several_shots(run_dir):
    with procs.run_scope("media.json") as run:
        with procs.shot_scope("a"), procs.shot_scope("b"):
            data = json.loads(run.path.read_text())
            assert data["shots"] == ["a", "b"] and data["shot"] == "a, b"
        assert json.loads(run.path.read_text())["shot"] is None


# ------------------------------------------------------------------ pilot: --adopt, sheet errors

def test_adopt_records_committed_outputs_without_shooting(write_manifest, fake_cmd, capsys):
    import shutil
    p = write_manifest({"shots": [
        {"name": "card", "backend": "command", "device": {"width": 32, "height": 32}, "format": "png",
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]},
        {"name": "walk", "kind": "clip", "backend": "command", "device": {"width": 32, "height": 32},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--frames", "3"]},
        {"name": "seasons", "backend": "command", "each": ["spring", "winter"], "device": {"width": 32, "height": 32},
         "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]},
        {"name": "never", "backend": "command", "argv": fake_cmd + ["{out_dir}", "8", "8"]}]})
    assert shoot(load(p), only=["card", "walk", "seasons"], log=quiet) == 0
    # A fresh checkout: the outputs are committed, the state and takes are not.
    shutil.rmtree(p.parent / ".darkroom")
    assert main(["shoot", str(p), "--adopt"]) == 0
    out = capsys.readouterr().out
    assert "= card: adopted (1 output(s))" in out and "= seasons: adopted (2 output(s))" in out
    assert "= walk: adopted (1 output(s))" in out  # the GIF
    assert "- never: not adopted: missing media/never.jpg" in out
    logs = []
    assert shoot(load(p), only=["card", "walk", "seasons"], log=logs.append) == 0
    assert all("up to date (adopted" in l for l in logs), logs
    # A change reshoots as usual.
    data = json.loads(p.read_text())
    data["shots"][0]["argv"] += ["--color", "1,2,3"]
    p.write_text(json.dumps(data))
    logs.clear()
    assert shoot(load(p), only=["card"], log=logs.append) == 0
    assert logs[0] == "> card (still, command)"


def test_sheet_on_a_missing_take_folder_points_at_the_takes(tmp_path, monkeypatch, capsys):
    take = tmp_path / "tools/capture/.darkroom/takes/walk"
    take.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert main(["sheet", ".darkroom/takes/walk"]) == 2
    err = capsys.readouterr().err
    assert "no such folder" in err and "tools/capture/.darkroom/takes/walk" in err and "Traceback" not in err
    assert main(["sheet", "nowhere/x"]) == 2
    assert "darkroom sheet MANIFEST SHOT" in capsys.readouterr().err
