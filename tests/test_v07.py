"""0.7: mouse steps (mouse, drag, wheel), a paced clock for real-time pages, and crops measured
from an element at capture time."""
import functools
import http.server
import json
import threading
from pathlib import Path

import pytest
from PIL import Image

from darkroom.backends.web import clock_job, playwright_available
from darkroom.manifest import Manifest, load, validate
from darkroom.shoot import shoot


def quiet(*_):
    pass


def problems(*shots, **top):
    return validate({"shots": list(shots), **top})


def steps(*sts, **shot):
    return problems({"name": "a", "url": "http://x/", "steps": list(sts), **shot})


# ------------------------------------------------------------------ compatibility

def test_hashes_of_06_shots_are_unchanged():
    # Values from darkroom 0.6.0 for the same manifest: 0.7 must not reshoot unchanged shots.
    data = {"vars": {"base": "http://localhost/"}, "defaults": {"device": "desktop"}, "shots": [
        {"name": "g", "kind": "clip", "url": "{base}", "crop": [0, 0, 320, 200], "frameStep": 0.05,
         "adapter": "strata-kit",
         "steps": [{"tap": {"at": [0.5, 0.5], "of": "canvas"}}, {"frames": 3, "step": 0.1}, {"capture": True}]},
        {"name": "h", "url": "{base}", "session": "s", "setup": [{"stable": "#b"}],
         "steps": [{"key": "KeyW", "action": "down"}, {"wait": 50}, {"capture": "x"}]},
        {"name": "i", "url": "{base}", "kind": "clip",
         "steps": [{"repeat": 3, "steps": [{"frames": 2}, {"capture": True}]}]}]}
    m = Manifest(Path("/nowhere/media.json"), data, dict(data["vars"]))
    got = {s.name: (m.capture_hash(s, None), m.encode_hash(s, "t")) for s in m.shots}
    assert got == {"g": ("b9987d4c02b11a1e", "fd23c50bdf44422f"), "h": ("9504b84ed3c4bba4", "fd23c50bdf44422f"),
                   "i": ("a79cab0be18f5583", "fd23c50bdf44422f")}
    # The new fields are capture fields: adding one reshoots.
    data["shots"][2]["clock"] = {"mode": "paced"}
    m2 = Manifest(Path("/nowhere/media.json"), data, dict(data["vars"]))
    assert m2.capture_hash(m2.shots[2], None) != got["i"][0]


# ------------------------------------------------------------------ validation

def test_mouse_drag_and_wheel_validation():
    assert steps({"mouse": "down", "at": [10, 20], "button": "right"}, {"mouse": "move", "by": [5, 0], "steps": 3},
                 {"mouse": "up", "button": "right"}, {"mouse": "move", "at": {"at": [0.5, 0.5], "of": "canvas"}},
                 {"mouse": "move", "at": "#btn"},
                 {"drag": {"from": {"at": [0.5, 0.5], "of": "canvas"}, "to": [400, 300], "steps": 20}},
                 {"wheel": {"dy": -120}, "at": [100, 100]}, {"wheel": {"dx": 30, "dy": 0}}) == []
    assert any("mouse: must be down, up or move" in x for x in steps({"mouse": "press"}))
    assert any("a move needs 'at'" in x for x in steps({"mouse": "move"}))
    assert any("not both" in x for x in steps({"mouse": "move", "at": [1, 2], "by": [1, 1]}))
    assert any("by: must be [dx, dy]" in x for x in steps({"mouse": "move", "by": 3}))
    assert any("button: must be one of left, right, middle" in x for x in steps({"mouse": "down", "button": "back"}))
    assert any("a move presses no button" in x for x in steps({"mouse": "move", "at": [1, 1], "button": "left"}))
    assert any("steps: must be a positive integer" in x for x in steps({"mouse": "move", "at": [1, 1], "steps": 0}))
    assert any("at.at: must be [fx, fy]" in x for x in steps({"mouse": "down", "at": {"at": [2, 0], "of": "c"}}))
    assert any("at: must be [x, y] in CSS pixels" in x for x in steps({"mouse": "down", "at": 5}))
    assert any("drag.to: required" in x for x in steps({"drag": {"from": [1, 1]}}))
    assert any("drag: unknown key 'step'" in x for x in steps({"drag": {"from": [1, 1], "to": [2, 2], "step": 3}}))
    assert any("drag: must be an object" in x for x in steps({"drag": [1, 1]}))
    assert any("wheel: must be {\"dx\": 0" in x for x in steps({"wheel": -120}))
    assert any("wheel.dy: must be a number" in x for x in steps({"wheel": {"dy": "up"}}))
    assert any("unknown step 'mous'" in x and "did you mean 'mouse'" in x for x in steps({"mous": "down"}))


def test_clock_validation():
    paced = {"mode": "paced", "step": 0.05}
    assert steps({"frames": 3, "step": 0.1}, clock=paced) == []
    assert steps({"frames": 3}, clock={"mode": "paced", "start": "load", "engine": "playwright"}) == []
    # Without a paced clock (or strata-kit) a frames step has no clock to step.
    assert any("only the strata-kit adapter or a paced clock" in x for x in steps({"frames": 3, "step": 0.1}))
    assert any("clock.mode: required, 'paced'" in x for x in steps(clock={"step": 0.05}))
    assert any("clock.step: must be a positive number" in x for x in steps(clock={"mode": "paced", "step": 0}))
    assert any("clock.start: must be 'ready'" in x for x in steps(clock={"mode": "paced", "start": "now"}))
    assert any("clock.engine: must be 'darkroom'" in x for x in steps(clock={"mode": "paced", "engine": "sinon"}))
    assert any("clock: unknown key 'fps'" in x for x in steps(clock={"mode": "paced", "fps": 20}))
    assert any("clock: must be an object" in x for x in steps(clock="paced"))
    assert any("strata-kit adapter already drives" in x for x in steps(clock=paced, adapter="strata-kit"))
    # Shots of one session share the page, so they share its clock.
    p = problems({"name": "a", "url": "http://x/", "session": "s", "clock": paced},
                 {"name": "b", "url": "http://x/", "session": "s"})
    assert any("clock" in x and "session 's'" in x for x in p)
    assert clock_job(None) is None
    assert clock_job({"mode": "paced"}) == {"mode": "paced", "step": 1 / 30, "start": "ready", "engine": "darkroom"}


def test_measured_crop_validation():
    assert steps(crop={"of": "#board", "pad": 8}) == []
    assert steps(crop={"of": "canvas"}) == []
    assert steps(crop=[0, 0, 10, 10]) == []
    assert any("crop.of: required" in x for x in steps(crop={"pad": 8}))
    assert any("crop.pad: must be a number" in x for x in steps(crop={"of": "c", "pad": "8px"}))
    assert any("crop: unknown key 'aspect'" in x for x in steps(crop={"of": "c", "aspect": "16:9"}))
    assert any("or {\"of\": \"css\", \"pad\": 8}" in x for x in steps(crop=[0, 0, 10]))
    assert any("only a web shot can measure" in x for x in problems(
        {"name": "a", "backend": "command", "argv": ["x", "{out_dir}"], "crop": {"of": "c"}}))


# ------------------------------------------------------------------ in the browser

PAGE = """<!doctype html><html><body style="margin:0;background:#fff;font:20px monospace">
<canvas id="spin" width="200" height="200" style="position:absolute;left:20px;top:20px"></canvas>
<div id="count" style="position:absolute;left:260px;top:40px;width:120px;height:30px;background:#ddd">0</div>
<div id="box" style="position:absolute;left:260px;top:120px;width:80px;height:50px;background:#36c"></div>
<script>
window.angle = 0; window.zoom = 1; window.downs = []; window.raf = 0; window.count = 0;
const spin = document.getElementById('spin'), g = spin.getContext('2d');
let dragging = null;
spin.addEventListener('contextmenu', (e) => e.preventDefault());
spin.addEventListener('mousedown', (e) => { downs.push(e.button); dragging = e.clientX; });
window.addEventListener('mousemove', (e) => { if (dragging !== null) { angle += (e.clientX - dragging) * 0.01; dragging = e.clientX; } });
window.addEventListener('mouseup', () => { dragging = null; });
spin.addEventListener('wheel', (e) => { zoom *= e.deltaY < 0 ? 1.25 : 0.8; e.preventDefault(); }, { passive: false });
setInterval(() => { count++; document.getElementById('count').textContent = String(count); }, 100);
(function frame() {
  raf++;
  g.fillStyle = '#123'; g.fillRect(0, 0, 200, 200);
  g.save(); g.translate(100, 100); g.rotate(angle); g.scale(zoom, zoom);
  g.fillStyle = '#e33'; g.fillRect(-40, -10, 80, 20); g.restore();
  requestAnimationFrame(frame);
})();
</script></body></html>"""

needs_browser = pytest.mark.skipif(not playwright_available(), reason="Node Playwright not resolvable (set NODE_PATH)")


@pytest.fixture
def server(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text(PAGE)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path / "site"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


DEVICE = {"width": 420, "height": 260}


@pytest.mark.browser
@needs_browser
def test_mouse_drag_and_wheel(server, write_manifest):
    p = write_manifest({"shots": [{
        "name": "spun", "url": server, "device": DEVICE, "retries": 0, "format": "png",
        "steps": [
            {"drag": {"from": {"at": [0.5, 0.5], "of": "#spin"}, "to": [220, 120], "steps": 5}},
            {"waitFor": "Math.abs(angle - 1) < 0.01 && downs.join() === '0'"},
            {"mouse": "down", "at": [120, 120], "button": "right"},
            {"repeat": 4, "steps": [{"mouse": "move", "by": [-10, 0]}]},
            {"mouse": "up", "button": "right"},
            {"waitFor": "Math.abs(angle - 0.6) < 0.01 && downs.join() === '0,2'"},
            {"wheel": {"dy": -120}, "at": "#spin"}, {"wheel": {"dy": -120}},
            {"waitFor": "Math.abs(zoom - 1.5625) < 1e-9"}]}]})
    assert shoot(load(p), log=quiet) == 0
    assert (p.parent / "media/spun.png").exists()


@pytest.mark.browser
@needs_browser
def test_paced_clock_is_deterministic_and_crop_is_measured(server, write_manifest):
    # Two fresh browsers (each), paced from the first script: every frame shows the same count in
    # both, however slowly the machine runs, and the crop is measured from #count.
    p = write_manifest({"shots": [{
        "name": "counter", "kind": "clip", "url": server, "device": DEVICE, "retries": 0, "each": ["a", "b"],
        "clock": {"mode": "paced", "step": 0.05, "start": "load"},
        "crop": {"of": "#count", "pad": 4}, "gif": {"fps": 4},
        "steps": [{"js": "window.seen = []; window.raf0 = raf"},
                  {"repeat": 4, "steps": [{"frames": 2}, {"wait": 150}, {"capture": True},
                                          {"js": "seen.push(count)"}]},
                  {"waitFor": "seen.join() === '1,2,3,4' && raf - raf0 === 8"}]}]})
    assert shoot(load(p), log=quiet) == 0
    meta = json.loads((p.parent / ".darkroom/takes/counter/meta.json").read_text())
    assert len(meta["frames"]) == 8
    assert meta["crops"] == [{"of": "#count", "pad": 4, "rect": [256, 36, 128, 38]}] * 2
    frames = p.parent / ".darkroom/takes/counter/frames"
    with Image.open(frames / meta["frames"][0]) as im:
        assert im.size == (128, 38)
    a = [(frames / f).read_bytes() for f in meta["frames"][:4]]
    b = [(frames / f).read_bytes() for f in meta["frames"][4:]]
    assert a == b and len(set(a)) == 4


@pytest.mark.browser
@needs_browser
@pytest.mark.parametrize("engine", ["darkroom", "playwright"])
def test_paced_clock_from_ready(server, write_manifest, engine):
    # start "ready": the page runs in real time until it is ready, then only frames steps move it.
    p = write_manifest({"shots": [{
        "name": "ready", "url": server, "device": DEVICE, "retries": 0, "ready": "window.count >= 2",
        "clock": {"mode": "paced", "step": 0.05, "engine": engine}, "crop": {"of": "#box"},
        "steps": [{"js": "window.c0 = count; window.t0 = performance.now(); window.d0 = Date.now()"},
                  {"wait": 400}, {"waitFor": "count === c0 && performance.now() === t0"},
                  {"frames": 10},
                  {"waitFor": "count === c0 + 5 && Math.abs(performance.now() - t0 - 500) < 1e-6"
                              " && Date.now() - d0 === 500 && new Date().getTime() === Date.now()"}]}]})
    assert shoot(load(p), log=quiet) == 0
    meta = json.loads((p.parent / ".darkroom/takes/ready/meta.json").read_text())
    assert meta["crop"] == {"of": "#box", "pad": 0, "rect": [260, 120, 80, 50]}


def test_type_steps_fill_vars():
    from darkroom.backends.web import template_steps
    out = template_steps([{"type": "{user}"}, {"repeat": 1, "steps": [{"type": "hi {user}"}]}], {"user": "ada"})
    assert out[0]["type"] == "ada" and out[1]["steps"][0]["type"] == "hi ada"


def test_setup_dir_is_searched(tmp_path, monkeypatch):
    from darkroom.backends import web
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    env = web.node_env(tmp_path)
    assert str(tmp_path / "darkroom" / "node_modules") in env["NODE_PATH"].split(":")


def test_setup_web_link_rejects_a_folder_without_playwright(tmp_path, monkeypatch, capsys):
    from darkroom.cli import main
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "nm").mkdir()
    assert main(["setup-web", "--link", str(tmp_path / "nm")]) == 2
    assert "no playwright" in capsys.readouterr().err
