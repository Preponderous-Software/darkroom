"""The web backend: Python builds a job, the bundled Node runner drives Playwright."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..manifest import template
from .command import CaptureError

RUNNER = Path(__file__).resolve().parent.parent / "web" / "runner.cjs"
LAUNCH_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
               "--ignore-gpu-blocklist"]
# Playwright's own default is 30 s, which a software-WebGL page on a loaded machine can miss
# on every attempt.
SCREENSHOT_TIMEOUT_MS = 120000


def action_timeout_ms(shot) -> int:
    """The timeout for every Playwright call that can time out (screenshots, clicks, taps,
    navigation): ``screenshotTimeout`` (ms), else the shot's ``timeout`` (s), else 120 s."""
    if shot.get("screenshotTimeout"):
        return int(shot["screenshotTimeout"])
    if shot.get("timeout"):
        return int(float(shot["timeout"]) * 1000)
    return SCREENSHOT_TIMEOUT_MS


def template_steps(steps, variables: dict):
    """Fill vars into ``goto`` URLs (other step text is JS, which sees ``vars`` itself)."""
    out = []
    for st in steps or []:
        if isinstance(st, dict):
            st = dict(st)
            if isinstance(st.get("goto"), str):
                st["goto"] = template(st["goto"], variables)
            if isinstance(st.get("steps"), list):
                st["steps"] = template_steps(st["steps"], variables)
        out.append(st)
    return out


def init_scripts(init, variables: dict, project: Path) -> list[str]:
    """``initScript`` as a list of JS sources, in order: strings are templated with vars (like
    ``url``); ``{"file": path}`` entries are read from the path (templated, relative to the
    manifest) as they are."""
    items = init if isinstance(init, list) else [init] if init is not None else []
    out = []
    for it in items:
        if isinstance(it, dict):
            path = project / str(template(it["file"], variables))
            try:
                out.append(path.read_text())
            except OSError as e:
                raise CaptureError(f"initScript: cannot read '{it['file']}': {e.strerror or e}")
        else:
            out.append(str(template(it, variables)))
    return out


def node_env(project_dir: Path) -> dict:
    env = dict(os.environ)
    paths = [str(project_dir / "node_modules")]
    if env.get("NODE_PATH"):
        paths.append(env["NODE_PATH"])
    env["NODE_PATH"] = os.pathsep.join(paths)
    return env


def playwright_available(project_dir: Path | str = ".") -> bool:
    node = shutil.which("node")
    if not node:
        return False
    p = subprocess.run([node, "-e", "require('playwright')"], env=node_env(Path(project_dir)),
                       cwd=str(project_dir), capture_output=True)
    return p.returncode == 0


def capture(shot, device: dict, variables: dict, frames_dir: Path, log=print) -> dict:
    """One browser run, or with ``each``, one fresh browser per value ({value}, {i}, {n} set in
    the url and in the page's ``vars``); frames are concatenated in order."""
    each = shot.get("each")
    if not each:
        result = _capture_once(shot, device, variables, frames_dir, log)
        result["runs"] = [0] * len(result["frames"])
        return result
    frames: list[str] = []
    runs: list[int] = []
    captions: list[str] = []
    errors: list[str] = []
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i, value in enumerate(each):
        sub = frames_dir / f"run-{i}"
        r = _capture_once(shot, device, dict(variables, value=value, i=i, n=len(each)), sub, log)
        if not r["frames"]:
            raise CaptureError(f"run {i + 1}/{len(each)} ({value}): no frames captured")
        for f in r["frames"]:
            dest = frames_dir / f"{len(frames):05d}{Path(f).suffix}"
            shutil.move(f, dest)
            frames.append(str(dest))
            runs.append(i)
            captions.append(str(value))
        shutil.rmtree(sub, ignore_errors=True)
        errors += r.get("consoleErrors") or []
    return {"frames": frames, "runs": runs, "captions": captions, "consoleErrors": errors}


def _capture_once(shot, device: dict, variables: dict, frames_dir: Path, log=print) -> dict:
    node = shutil.which("node")
    if not node:
        raise CaptureError("the web backend needs Node.js (node on PATH) and Playwright")
    frames_dir.mkdir(parents=True, exist_ok=True)
    project = Path(variables.get("manifest_dir", "."))
    timeout = float(shot.get("timeout", 300))
    with tempfile.TemporaryDirectory(prefix="darkroom-web-") as tmp:
        job = {
            "url": template(shot["url"], variables),
            "kind": shot.kind,
            "adapter": shot.get("adapter", "hook"),
            "ready": template(shot.get("ready"), variables) if shot.get("ready") else None,
            "readyTimeout": int(shot.get("readyTimeout", 120000)),
            "waitUntil": shot.get("waitUntil", "load"),
            "css": shot.get("css"),
            "setup": template_steps(shot.get("setup", []), variables),
            "steps": template_steps(shot.get("steps", []), variables),
            "timeoutMs": action_timeout_ms(shot),
            "frameStep": float(shot.get("frameStep", 1 / 30)),
            "device": device,
            "crop": shot.get("crop"),
            "userAgent": shot.get("userAgent"),
            "block": shot.get("block", []),
            "vars": {k: v for k, v in variables.items() if k != "manifest_dir"},
            "launchArgs": shot.get("launchArgs", LAUNCH_ARGS),
            "initScripts": init_scripts(shot.get("initScript"), variables, project),
            "context": template(shot.get("context") or {}, variables),
            "framesDir": str(frames_dir),
            "resultPath": str(Path(tmp) / "result.json"),
            "cwd": str(project),
        }
        try:
            p = subprocess.run([node, str(RUNNER)], input=json.dumps(job), text=True, capture_output=True,
                               env=node_env(project), cwd=str(project), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CaptureError(f"browser run timed out after {timeout:g}s")
        try:
            result = json.loads(Path(job["resultPath"]).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            raise CaptureError(f"runner died (exit {p.returncode}): {(p.stderr or p.stdout)[-800:]}")
    if result.get("error"):
        errs = result.get("consoleErrors") or []
        raise CaptureError(result["error"] + (f"\npage errors: {errs[:5]}" if errs else ""))
    marks = result.get("marks", {})
    frames = result["frames"]
    names = result.get("names") or [None] * len(frames)
    if marks:
        frames = frames[marks.get("in", 0):marks.get("out", len(frames))]
        names = names[marks.get("in", 0):marks.get("out", len(names))]
    result["frames"] = frames
    result["names"] = names
    return result
