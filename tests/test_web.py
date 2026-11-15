"""One browser test against a tiny local page with a fake __strataKit. Skipped without Node Playwright."""
import functools
import http.server
import json
import threading
from pathlib import Path

import pytest
from PIL import Image

from darkroom.backends.web import playwright_available
from darkroom.manifest import load
from darkroom.shoot import shoot

PAGE = """<!doctype html><html><body style="margin:0;background:#000">
<canvas id="c" width="320" height="180"></canvas><div id="hud">HUD</div>
<button id="btn" style="position:absolute;left:200px;top:120px;width:60px;height:30px">go</button>
<script>
const tapped = () => { window.tapped = (window.tapped || 0) + 1; };
document.getElementById('btn').addEventListener('click', tapped);
const c = document.getElementById('c').getContext('2d');
const cam = { yaw: 0, pitch: 0, dist: 5, tx: 0, ty: 0, tz: 0 };
function draw(t) {
  c.fillStyle = '#123'; c.fillRect(0, 0, 320, 180);
  c.fillStyle = '#e33'; c.fillRect(20 + (t * 60) % 260, 60 + cam.yaw * 10, 40, 40);
}
window.__strataKit = { cam, drive: (t) => { window.lastT = t; draw(t); } };
draw(0);
</script></body></html>"""

pytestmark = pytest.mark.browser


@pytest.fixture
def server(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text(PAGE)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path / "site"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


@pytest.mark.skipif(not playwright_available(), reason="Node Playwright not resolvable (set NODE_PATH)")
def test_strata_kit_clip(server, write_manifest):
    p = write_manifest({"vars": {"base": server}, "shots": [{
        "name": "slide", "kind": "clip", "adapter": "strata-kit", "url": "{base}index.html",
        "device": {"width": 320, "height": 180}, "css": "#hud{display:none}", "retries": 0,
        "steps": [{"camera": {"yaw": 2}},
                  {"repeat": 4, "steps": [{"js": "window.seen = (window.seen || 0) + i + vars.k.length"},
                                          {"frames": 3}, {"capture": True}]},
                  {"waitFor": "window.seen === 4 * 1 + 6"}],
        "gif": {"fps": 4}}]})
    p_vars = load(p, {"k": "x"})
    assert shoot(p_vars) == 0
    meta = json.loads((p.parent / ".darkroom/takes/slide/meta.json").read_text())
    assert len(meta["frames"]) == 4
    with Image.open(p.parent / "media/slide.gif") as g:
        assert g.n_frames == 4


@pytest.mark.skipif(not playwright_available(), reason="Node Playwright not resolvable (set NODE_PATH)")
def test_each_runs_a_fresh_page_per_value(server, write_manifest):
    p = write_manifest({"vars": {"base": server}, "shots": [{
        "name": "view", "kind": "still", "adapter": "strata-kit", "url": "{base}index.html?v={value}",
        "device": {"width": 320, "height": 180}, "retries": 0, "each": ["a", "b"],
        "steps": [{"waitFor": "location.search === '?v=' + vars.value && vars.n === 2"}]}]})
    assert shoot(load(p)) == 0
    assert (p.parent / "media/view-a.jpg").exists() and (p.parent / "media/view-b.jpg").exists()
    meta = json.loads((p.parent / ".darkroom/takes/view/meta.json").read_text())
    assert meta["runs"] == [0, 1] and meta["captions"] == ["a", "b"]


@pytest.mark.skipif(not playwright_available(), reason="Node Playwright not resolvable (set NODE_PATH)")
def test_steps_added_in_0_3(server, write_manifest):
    """tap on a selector (touch), frames with a step, until/then, goto, reload, named stills;
    and a waitFor that never holds fails the shot instead of passing at once."""
    touch = {"width": 320, "height": 180, "touch": True, "mobile": True}
    p = write_manifest({"vars": {"base": server}, "shots": [
        {"name": "tour", "kind": "still", "adapter": "strata-kit", "url": "{base}index.html",
         "device": touch, "retries": 0, "frameStep": 0.05, "steps": [
             {"tap": "#btn"}, {"waitFor": "window.tapped === 1", "timeout": 10000},
             {"frames": 3, "step": 0.1}, {"frames": 2},
             {"waitFor": "Math.abs(window.lastT - 1.4) < 1e-9", "timeout": 5000},
             {"until": "window.k >= 3", "max": 10, "then": 2, "steps": [{"js": "window.k = (window.k || 0) + 1"}]},
             {"waitFor": "window.k === 5", "timeout": 5000},
             {"capture": "tour-first"},
             {"goto": "{base}index.html?second=1"},
             {"js": "window.k = 9"},
             {"reload": True},
             {"waitFor": "location.search === '?second=1' && window.k === undefined", "timeout": 5000},
             {"capture": "tour-second"}]},
        {"name": "never", "kind": "still", "url": "{base}index.html", "retries": 0,
         "device": {"width": 160, "height": 90}, "steps": [{"waitFor": "false", "timeout": 1500}]}]})
    logs = []
    assert shoot(load(p), log=logs.append) == 1
    assert any("never: FAILED" in l for l in logs) and not any("tour: FAILED" in l for l in logs), logs
    assert (p.parent / "media/tour-first.jpg").exists() and (p.parent / "media/tour-second.jpg").exists()
    assert not (p.parent / "media/tour.jpg").exists()
    meta = json.loads((p.parent / ".darkroom/takes/tour/meta.json").read_text())
    assert meta["names"] == ["tour-first", "tour-second"]


@pytest.mark.skipif(not playwright_available(), reason="Node Playwright not resolvable (set NODE_PATH)")
def test_init_script_and_context(server, write_manifest):
    """initScript runs before the page's own scripts (a string templated with vars, then a file);
    context options reach browser.newContext."""
    (write_manifest({}, "unused.json").parent / "init.js").write_text(
        "window.order = (window.order || []).concat('file');")
    p = write_manifest({"vars": {"base": server, "q": "high"}, "shots": [{
        "name": "seeded", "kind": "still", "url": "{base}index.html", "retries": 0,
        "device": {"width": 160, "height": 90},
        "initScript": ["window.pre = typeof window.__strataKit; localStorage.setItem('q', '{q}');"
                       " window.order = ['inline'];", {"file": "init.js"}],
        "context": {"reducedMotion": "reduce", "locale": "de-DE", "colorScheme": "dark"},
        "steps": [{"waitFor": "window.pre === 'undefined' && localStorage.getItem('q') === 'high'"
                              " && window.order.join() === 'inline,file'"
                              " && matchMedia('(prefers-reduced-motion: reduce)').matches"
                              " && matchMedia('(prefers-color-scheme: dark)').matches"
                              " && navigator.language === 'de-DE'", "timeout": 5000}]}]})
    logs = []
    assert shoot(load(p), log=logs.append) == 0, logs
    assert (p.parent / "media/seeded.jpg").exists()


# ------------------------------------------------------------------ 0.6

STEPS_PAGE = """<!doctype html><html><body style="margin:0">
<canvas id="c" width="320" height="180" style="display:block"></canvas>
<div id="mover" style="position:absolute;left:0;top:200px;width:40px;height:20px;background:#f80"></div>
<button id="spin" style="position:absolute;left:100px;top:240px;width:80px;height:40px;
  animation:spin 0.5s linear infinite">spin</button>
<style>@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}</style>
<script>
document.getElementById('c').addEventListener('click', (e) => { window.hit = [e.clientX, e.clientY]; });
document.getElementById('spin').addEventListener('click', () => { window.spun = (window.spun || 0) + 1; });
const mover = document.getElementById('mover'), t0 = performance.now();
(function step(t) {
  const x = Math.min(200, (t - t0) / 3);
  mover.style.left = x + 'px';
  if (x < 200) requestAnimationFrame(step); else window.moverDone = true;
})(t0);
console.warn('low on memory (pretend)');
console.log('booted', 1);
</script></body></html>"""


def _site(server, tmp_path, name, text):
    (tmp_path / "site" / name).write_text(text)
    return server + name


@pytest.mark.skipif(not playwright_available(), reason="Node Playwright not resolvable (set NODE_PATH)")
def test_console_log_stable_tap_at_and_force(server, tmp_path, write_manifest):
    url = _site(server, tmp_path, "steps.html", STEPS_PAGE)
    dev = {"width": 320, "height": 300}
    p = write_manifest({"shots": [
        {"name": "steps", "url": url, "device": dev, "retries": 0, "steps": [
            {"stable": "#mover", "timeout": 10000}, {"waitFor": "window.moverDone === true", "timeout": 1000},
            {"tap": {"at": [0.25, 0.5], "of": "#c"}},
            {"waitFor": "window.hit && window.hit[0] === 80 && window.hit[1] === 90", "timeout": 5000},
            {"click": {"at": [0.5, 0.1], "of": "#c"}},
            {"waitFor": "window.hit[0] === 160 && window.hit[1] === 18", "timeout": 5000},
            {"click": "#spin", "force": True}, {"waitFor": "window.spun === 1", "timeout": 5000}]},
        {"name": "strict", "url": url, "device": dev, "retries": 0, "failOnConsole": ["warning"]}]})
    logs = []
    assert shoot(load(p), log=logs.append) == 1
    assert any("strict: FAILED" in l and "1 console warning(s) and failOnConsole is set" in l for l in logs), logs
    assert not any("steps: FAILED" in l for l in logs), logs
    lines = [json.loads(l) for l in (p.parent / ".darkroom/takes/steps/console.jsonl").read_text().splitlines()]
    by_type = {c["type"]: c for c in lines}
    assert by_type["warning"]["text"] == "low on memory (pretend)"
    assert by_type["log"]["text"] == "booted 1"
    assert by_type["log"]["location"]["url"].endswith("steps.html") and "T" in by_type["log"]["time"]
    meta = json.loads((p.parent / ".darkroom/takes/steps/meta.json").read_text())
    assert meta["console"] == {"warning": 1, "log": 1} and meta["consoleErrors"] == []


@pytest.mark.skipif(not playwright_available(), reason="Node Playwright not resolvable (set NODE_PATH)")
def test_session_shares_one_page_and_a_failure_starts_fresh(server, write_manifest):
    p = write_manifest({"vars": {"base": server}, "defaults": {
        "url": "{base}index.html", "session": "game", "device": {"width": 160, "height": 90}, "retries": 0,
        "setup": [{"js": "window.setups = (window.setups || 0) + 1"}]}, "shots": [
        {"name": "one", "steps": [{"js": "window.k = 1"}]},
        {"name": "two", "steps": [{"waitFor": "window.k === 1 && window.setups === 1", "timeout": 3000},
                                  {"js": "window.k = 2"}]},
        {"name": "broken", "steps": [{"waitFor": "false", "timeout": 500}]},
        {"name": "fresh", "steps": [{"waitFor": "window.k === undefined && window.setups === 1", "timeout": 3000}]},
        {"name": "alone", "session": "other", "steps": [{"waitFor": "window.k === undefined", "timeout": 3000}]}]})
    logs = []
    assert shoot(load(p), log=logs.append) == 1, logs
    assert any("broken: FAILED" in l for l in logs)
    assert sum("session game: opening the page" in l for l in logs) == 2, logs  # once, then after the failure
    for name in ("one", "two", "fresh", "alone"):
        assert (p.parent / f"media/{name}.jpg").exists(), name
        assert json.loads((p.parent / f".darkroom/takes/{name}/meta.json").read_text()).get("session") in ("game", "other")
    # state and hashes stay per shot: a second run skips them all, without opening a page
    logs.clear()
    assert shoot(load(p), only=["one", "two"], log=logs.append) == 0
    assert all(l.startswith("= ") for l in logs), logs


@pytest.mark.skipif(not playwright_available(), reason="Node Playwright not resolvable (set NODE_PATH)")
def test_each_over_devices_with_named_captures(server, write_manifest):
    p = write_manifest({"vars": {"base": server}, "devices": {"small": {"width": 120, "height": 90},
                                                               "wide": {"width": 200, "height": 90}},
                        "shots": [{"name": "screens", "url": "{base}index.html", "retries": 0,
                                   "each": ["small", "wide"], "device": "{value}",
                                   "steps": [{"capture": "title-{value}"}, {"js": "window.x = 1"},
                                             {"capture": "map-{value}"}]}]})
    assert shoot(load(p)) == 0
    for name, w in (("title-small", 120), ("map-small", 120), ("title-wide", 200), ("map-wide", 200)):
        with Image.open(p.parent / f"media/{name}.jpg") as im:
            assert im.size == (w, 90), name
    state = json.loads((p.parent / ".darkroom/state.json").read_text())
    assert [(c["row"], c["device"]) for c in state["screens"]["cells"]] == [
        ("title", "small"), ("map", "small"), ("title", "wide"), ("map", "wide")]
