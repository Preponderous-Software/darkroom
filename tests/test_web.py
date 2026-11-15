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
<script>
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
