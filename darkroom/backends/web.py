"""The web backend: Python builds a job, the bundled Node runner drives Playwright."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import procs
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
    """Fill vars into ``goto`` URLs and capture names (other step text is JS, which sees ``vars``
    itself)."""
    out = []
    for st in steps or []:
        if isinstance(st, dict):
            st = dict(st)
            if isinstance(st.get("goto"), str):
                st["goto"] = template(st["goto"], variables)
            if isinstance(st.get("capture"), str):  # capture names may use vars ("menu-{value}")
                st["capture"] = template(st["capture"], variables)
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
    try:
        p = procs.run([node, "-e", "require('playwright')"], env=node_env(Path(project_dir)),
                      cwd=str(project_dir), timeout=60)
    except subprocess.TimeoutExpired:
        return False
    return p.returncode == 0


def capture(shot, device: dict, variables: dict, frames_dir: Path, log=print, device_for=None,
            session: "WebSession | None" = None) -> dict:
    """One browser run, or with ``each``, one fresh browser per value ({value}, {i}, {n} set in
    the url and in the page's ``vars``); frames are concatenated in order. ``device_for(vars)``
    gives each run's device when it changes per value (``"device": "{value}"``). With a
    ``session``, the shot's steps run on the session's open page instead."""
    each = shot.get("each")
    if session is not None:
        result = session.shot(shot, device, variables, frames_dir, log)
        result["runs"] = [0] * len(result["frames"])
        return result
    if not each:
        result = _capture_once(shot, device, variables, frames_dir, log)
        result["runs"] = [0] * len(result["frames"])
        return result
    frames: list[str] = []
    runs: list[int] = []
    captions: list[str] = []
    names: list[str | None] = []
    errors: list[str] = []
    console: list[dict] = []
    warnings: list[str] = []
    crops: list[dict | None] = []
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i, value in enumerate(each):
        sub = frames_dir / f"run-{i}"
        run_vars = dict(variables, value=value, i=i, n=len(each))
        dev = device_for(run_vars) if device_for else device
        r = _capture_once(shot, dev, run_vars, sub, log)
        if not r["frames"]:
            raise CaptureError(f"run {i + 1}/{len(each)} ({value}): no frames captured")
        for f, name in zip(r["frames"], r.get("names") or [None] * len(r["frames"])):
            if name and name in names:
                raise CaptureError(f"run {i + 1}/{len(each)} ({value}): capture name '{name}' was already "
                                   f"written by an earlier run; put {{value}} in the capture name")
            dest = frames_dir / f"{len(frames):05d}{Path(f).suffix}"
            shutil.move(f, dest)
            frames.append(str(dest))
            runs.append(i)
            captions.append(str(value))
            names.append(name)
        shutil.rmtree(sub, ignore_errors=True)
        errors += r.get("consoleErrors") or []
        console += [dict(c, run=i) for c in r.get("console") or []]
        warnings += r.get("warnings") or []
        crops.append(r.get("crop"))
    out = {"frames": frames, "runs": runs, "captions": captions, "consoleErrors": errors,
           "console": console, "names": names, "warnings": warnings}
    if any(crops):
        out["crops"] = crops
    return out


def clock_job(clock) -> dict | None:
    """The shot's ``clock`` with its defaults filled in (None: the page's own clock)."""
    if not clock:
        return None
    return {"mode": clock.get("mode", "paced"), "step": float(clock.get("step", 1 / 30)),
            "start": clock.get("start", "ready"), "engine": clock.get("engine", "darkroom")}


def _job(shot, device: dict, variables: dict, frames_dir: Path, project: Path, result_path: str | None) -> dict:
    return {
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
        "clock": clock_job(shot.get("clock")),
        "device": device,
        "crop": shot.get("crop"),
        "userAgent": shot.get("userAgent"),
        "block": shot.get("block", []),
        "vars": {k: v for k, v in variables.items() if k != "manifest_dir"},
        "launchArgs": shot.get("launchArgs", LAUNCH_ARGS),
        "initScripts": init_scripts(shot.get("initScript"), variables, project),
        "context": template(shot.get("context") or {}, variables),
        "framesDir": str(frames_dir),
        "resultPath": result_path,
        "cwd": str(project),
    }


def _finish(result: dict) -> dict:
    """Raise a runner error; trim frames to the in/out marks."""
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


def _capture_once(shot, device: dict, variables: dict, frames_dir: Path, log=print) -> dict:
    node = shutil.which("node")
    if not node:
        raise CaptureError("the web backend needs Node.js (node on PATH) and Playwright")
    frames_dir.mkdir(parents=True, exist_ok=True)
    project = Path(variables.get("manifest_dir", "."))
    timeout = float(shot.get("timeout", 300))
    with tempfile.TemporaryDirectory(prefix="darkroom-web-") as tmp:
        job = _job(shot, device, variables, frames_dir, project, str(Path(tmp) / "result.json"))
        try:
            # The runner (and the Chromium it launches) in its own process group: on a timeout,
            # a stop or Ctrl-C the whole browser tree is killed, not just Node.
            p = procs.run([node, str(RUNNER)], input=json.dumps(job), text=True,
                          env=node_env(project), cwd=str(project), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CaptureError(f"browser run timed out after {timeout:g}s")
        try:
            result = json.loads(Path(job["resultPath"]).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            raise CaptureError(f"runner died (exit {p.returncode}): {(p.stderr or p.stdout)[-800:]}")
    return _finish(result)


class WebSession:
    """One browser page shared by consecutive shots with the same ``session`` (#17).

    The page is opened (url, ready, css, ``setup``) by the first shot that needs a capture, with
    that shot's values; then each shot's ``steps`` run on it in turn. ``close()`` ends it; the next
    shot opens a fresh one. Opening takes the shot's ``lock`` and holds it until the session
    closes, so a heavy page counts as one capture. The runner is spawned through ``procs``, so
    it is in its own process group and registered with the run."""

    PREFIX = b"@@darkroom "

    def __init__(self, name: str, log=print):
        self.name = name
        self.log = log
        self.p = None
        self._buf = b""
        self._stderr = None
        self._lock = None  # an ExitStack holding the session's lock

    @property
    def open(self) -> bool:
        return self.p is not None

    def _start(self, shot, device: dict, variables: dict, frames_dir: Path, timeout: float, hold_lock) -> None:
        import contextlib
        node = shutil.which("node")
        if not node:
            raise CaptureError("the web backend needs Node.js (node on PATH) and Playwright")
        project = Path(variables.get("manifest_dir", "."))
        stack = contextlib.ExitStack()
        try:
            if hold_lock is not None:
                stack.enter_context(hold_lock())
            self._stderr = stack.enter_context(tempfile.TemporaryFile())
            self._lock = stack
            self.p = procs.spawn([node, str(RUNNER), "--session"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=self._stderr, env=node_env(project),
                                 cwd=str(project))
            self._buf = b""
            frames_dir.mkdir(parents=True, exist_ok=True)
            self._send(_job(shot, device, variables, frames_dir, project, None))
            reply = self._recv(timeout, "opening the page")
        except BaseException:
            self.close()
            raise
        if not reply.get("ready"):
            self.close()
            errs = reply.get("consoleErrors") or []
            raise CaptureError(f"session '{self.name}': opening the page failed: {reply.get('error')}"
                               + (f"\npage errors: {errs[:5]}" if errs else ""))
        for w in reply.get("warnings") or []:
            self.log(f"  session {self.name}: {w}")

    def shot(self, shot, device: dict, variables: dict, frames_dir: Path, log=print, hold_lock=None) -> dict:
        timeout = float(shot.get("timeout", 300))
        frames_dir.mkdir(parents=True, exist_ok=True)
        if not self.open:
            log(f"  session {self.name}: opening the page")
            self._start(shot, device, variables, frames_dir, timeout, hold_lock)
        project = Path(variables.get("manifest_dir", "."))
        job = _job(shot, device, variables, frames_dir, project, None)
        msg = {"cmd": "shot", "kind": job["kind"], "steps": job["steps"], "crop": job["crop"],
               "vars": job["vars"], "framesDir": job["framesDir"]}
        try:
            self._send(msg)
            result = self._recv(timeout, "running the steps")
        except BaseException:
            self.close()
            raise
        return _finish(result)

    def _send(self, obj) -> None:
        try:
            self.p.stdin.write((json.dumps(obj) + "\n").encode())
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            raise CaptureError(f"session '{self.name}': the browser runner died{self._tail()}")

    def _recv(self, timeout: float, doing: str) -> dict:
        import select
        import time
        deadline = time.monotonic() + timeout
        fd = self.p.stdout.fileno()
        while True:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                if line.startswith(self.PREFIX):
                    return json.loads(line[len(self.PREFIX):])
            left = deadline - time.monotonic()
            if left <= 0:
                raise CaptureError(f"session '{self.name}': {doing} timed out after {timeout:g}s")
            ready, _, _ = select.select([fd], [], [], min(left, 1.0))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise CaptureError(f"session '{self.name}': the browser runner died while {doing}{self._tail()}")
            self._buf += chunk

    def _tail(self) -> str:
        try:
            self._stderr.seek(0)
            text = self._stderr.read().decode(errors="replace").strip()
        except (OSError, ValueError, AttributeError):
            return ""
        return f": {text[-800:]}" if text else ""

    def close(self) -> None:
        """End the session: ask the runner to close the browser, then reap its process group."""
        p, self.p = self.p, None
        try:
            if p is not None:
                try:
                    p.stdin.write(b'{"cmd": "close"}\n')
                    p.stdin.flush()
                    p.stdin.close()
                    p.wait(10)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                procs.reap(p)
                for f in (p.stdout,):
                    try:
                        f.close()
                    except OSError:
                        pass
        finally:
            stack, self._lock = self._lock, None
            if stack is not None:
                stack.close()
