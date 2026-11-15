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
            "setup": shot.get("setup", []),
            "steps": shot.get("steps", []),
            "device": device,
            "crop": shot.get("crop"),
            "userAgent": shot.get("userAgent"),
            "block": shot.get("block", []),
            "vars": {k: v for k, v in variables.items() if k != "manifest_dir"},
            "launchArgs": shot.get("launchArgs", LAUNCH_ARGS),
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
    if marks:
        frames = frames[marks.get("in", 0):marks.get("out", len(frames))]
    result["frames"] = frames
    return result
