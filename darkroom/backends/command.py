"""The command backend: run any program that writes images into a directory.

Each run gets its own temporary ``{out_dir}`` (where images are collected from), a sibling
scratch ``{work_dir}`` that is the default working directory (so whatever else the program
writes, such as config or cache files, never lands in the project) and, with ``xvfb: true``,
its own Xvfb display (picked by Xvfb itself via ``-displayfd``), so parallel captures never
collide.
"""
from __future__ import annotations

import os
import select
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from ..manifest import expand_argv, template

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


class CaptureError(Exception):
    pass


# Xvfb's own GLX module loads the host's GL driver; some drivers (e.g. NVIDIA's under WSL) crash
# it. Software GL (Mesa llvmpipe) in Xvfb's environment keeps GLX working, and the program then
# gets real OpenGL on the display. `xvfb: {"env": {...}}` replaces these; `{"env": {}}` drops them.
XVFB_ENV = {"LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "llvmpipe"}


@contextmanager
def xvfb(width: int, height: int, args=(), timeout: float = 30, log=print, env=None):
    """Start a private Xvfb sized to the shot; yield its display string (e.g. ':57').

    Xvfb picks a free display itself (``-displayfd``), so parallel captures never collide.
    Xvfb runs with software GL (``XVFB_ENV``) unless ``env`` says otherwise. If it still dies
    at startup, it is started once more without GLX, with a warning."""
    env = XVFB_ENV if env is None else env
    try:
        with _xvfb(width, height, list(args), timeout, env) as d:
            yield d
            return
    except _XvfbDied as e:
        if "-extension" in args:
            raise CaptureError(str(e))
        log(f"  Xvfb died at startup ({e}); retrying with -extension GLX (no OpenGL on this display)")
    with _xvfb(width, height, list(args) + ["-extension", "GLX"], timeout, env) as d:
        yield d


class _XvfbDied(CaptureError):
    pass


@contextmanager
def _xvfb(width: int, height: int, args: list, timeout: float, env: dict):
    exe = shutil.which("Xvfb")
    if not exe:
        raise CaptureError("xvfb: true but Xvfb is not installed (apt install xvfb)")
    r, w = os.pipe()
    proc = subprocess.Popen([exe, "-displayfd", str(w), "-screen", "0", f"{width}x{height}x24",
                             "-nolisten", "tcp", "-noreset", *args], pass_fds=(w,), env={**os.environ, **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(w)
    try:
        buf = b""
        deadline = time.monotonic() + timeout
        while not buf.endswith(b"\n"):
            left = deadline - time.monotonic()
            if left <= 0 or proc.poll() is not None:
                raise _XvfbDied("Xvfb did not start" + (f" (exit {proc.returncode})" if proc.returncode is not None else ""))
            ready, _, _ = select.select([r], [], [], left)
            if ready:
                chunk = os.read(r, 32)
                if not chunk:
                    raise _XvfbDied("Xvfb exited without a display number")
                buf += chunk
        yield ":" + buf.decode().strip()
    finally:
        os.close(r)
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _collect(out_dir: Path, glob: str | None) -> list[Path]:
    files = sorted(out_dir.rglob(glob)) if glob else sorted(
        p for p in out_dir.rglob("*") if p.suffix.lower() in IMAGE_EXTS)
    return [p for p in files if p.is_file()]


def capture(shot, device: dict, variables: dict, frames_dir: Path, log=print) -> dict:
    """Run the shot's argv once per ``each`` value (or ``repeat`` times), with {i}/{n}/{value};
    copy every image it writes into ``frames_dir`` in order. Returns {"frames", "runs",
    "captions", "logs"}: ``runs`` and ``captions`` give each frame's run index and ``each`` value."""
    each = shot.get("each")
    repeat = len(each) if each else int(shot.get("repeat", 1))
    timeout = float(shot.get("timeout", 600))
    frames: list[str] = []
    runs: list[int] = []
    captions: list[str | None] = []
    logs: list[str] = []
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i in range(repeat):
        with tempfile.TemporaryDirectory(prefix=f"darkroom-{shot.name}-") as tmp:
            out_dir, work_dir = Path(tmp) / "out", Path(tmp) / "work"
            out_dir.mkdir()
            work_dir.mkdir()
            v = dict(variables)
            v.update({"out_dir": str(out_dir), "work_dir": str(work_dir), "width": device["width"],
                      "height": device["height"], "dpr": device.get("dpr", 1), "name": shot.name,
                      "i": i, "n": repeat})
            if each:
                v["value"] = each[i]
            xv = shot.get("xvfb")
            if xv:
                xargs = xv.get("args", []) if isinstance(xv, dict) else []
                xenv = xv.get("env") if isinstance(xv, dict) else None
                xenv = XVFB_ENV if xenv is None else template(xenv, v)
                with xvfb(device["width"], device["height"], xargs, log=log, env=xenv) as display:
                    v["display"] = display
                    _run(shot, v, timeout, logs, display, xenv)
            else:
                v.setdefault("display", os.environ.get("DISPLAY", ""))
                _run(shot, v, timeout, logs, None)
            named = shot.named_outputs()
            if named:
                got = _named(out_dir, named, v, logs)
                names = [k for k, _ in named]
            else:
                got = _collect(out_dir, shot.get("glob"))
                names = []
            if not got:
                raise CaptureError(f"run {i + 1}/{repeat}: the command wrote no images into {{out_dir}}"
                                   + (f"; last output: {logs[-1][-400:]}" if logs else ""))
            for f in got:
                dest = frames_dir / f"{len(frames):05d}{f.suffix.lower()}"
                shutil.copyfile(f, dest)
                frames.append(str(dest))
                runs.append(i)
                captions.append(str(each[i]) if each else None)
    out = {"frames": frames, "runs": runs, "captions": captions, "logs": logs, "consoleErrors": []}
    if repeat == 1 and names:
        out["names"] = names
    return out


def _named(out_dir: Path, named, v: dict, logs: list[str]) -> list[Path]:
    """The files a command still's ``outputs`` name, in order; every one must exist."""
    got, absent = [], []
    for key, o in named:
        f = out_dir / str(template(o["file"], v))
        if f.is_file():
            got.append(f)
        else:
            absent.append(f"outputs.{key}: '{o['file']}'")
    if absent:
        wrote = sorted(str(p.relative_to(out_dir)) for p in out_dir.rglob("*") if p.is_file())
        raise CaptureError("the command did not write " + ", ".join(absent) + " into {out_dir}"
                           + (f" (it wrote: {', '.join(wrote[:10])})" if wrote else " (it wrote nothing there)")
                           + (f"; last output: {logs[-1][-400:]}" if logs and logs[-1] else ""))
    return got


def run_prep(shot, device: dict, variables: dict, log=print) -> list[str]:
    """Run a prep's argv once. Its default working directory is the manifest's folder (it exists
    to write files there); {out_dir} and {work_dir} are scratch dirs as for any command."""
    timeout = float(shot.get("timeout", 600))
    logs: list[str] = []
    with tempfile.TemporaryDirectory(prefix=f"darkroom-{shot.name}-") as tmp:
        out_dir, work_dir = Path(tmp) / "out", Path(tmp) / "work"
        out_dir.mkdir()
        work_dir.mkdir()
        v = dict(variables)
        v.update({"out_dir": str(out_dir), "work_dir": str(work_dir), "width": device["width"],
                  "height": device["height"], "dpr": device.get("dpr", 1), "name": shot.name, "i": 0, "n": 1})
        xv = shot.get("xvfb")
        if xv:
            xargs = xv.get("args", []) if isinstance(xv, dict) else []
            xenv = xv.get("env") if isinstance(xv, dict) else None
            xenv = XVFB_ENV if xenv is None else template(xenv, v)
            with xvfb(device["width"], device["height"], xargs, log=log, env=xenv) as display:
                v["display"] = display
                _run(shot, v, timeout, logs, display, xenv, default_cwd="{manifest_dir}")
        else:
            v.setdefault("display", os.environ.get("DISPLAY", ""))
            _run(shot, v, timeout, logs, None, default_cwd="{manifest_dir}")
    return logs


def anchor_paths(argv: list[str], manifest_dir: str) -> list[str]:
    """The default cwd is the run's scratch dir, so relative paths that meant the manifest's
    directory (when it was the cwd, in 0.1) are made absolute: the program if it has a slash
    (``./build/Game``), and any other entry that names an existing file next to the manifest
    (``render.py``), or an existing path when it has a slash (``scenes/title.json``)."""
    out = []
    for k, a in enumerate(argv):
        if a and not os.path.isabs(a) and not a.startswith("-"):
            cand = os.path.normpath(os.path.join(manifest_dir, a))
            if ("/" in a and (k == 0 or os.path.exists(cand))) or (k > 0 and os.path.isfile(cand)):
                a = cand
        out.append(a)
    return out


def _run(shot, v, timeout, logs, display, gl_env=None, default_cwd="{work_dir}"):
    argv = expand_argv(shot["argv"], v)
    if shot.get("cwd") is None and default_cwd == "{work_dir}":
        argv = anchor_paths(argv, v.get("manifest_dir", "."))
    env = dict(os.environ)
    env.update(gl_env or {})  # the same GL as the display's (the shot's own env still wins)
    env.update(template(shot.get("env", {}), v))
    if display:
        env["DISPLAY"] = display
    cwd = template(shot.get("cwd", default_cwd), v)
    if not os.path.isabs(cwd):
        cwd = os.path.join(v.get("manifest_dir", "."), cwd)
    try:
        p = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CaptureError(f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        raise CaptureError(f"command timed out after {timeout:g}s: {' '.join(argv)}")
    out = (p.stdout + p.stderr).strip()
    logs.append(out)
    if p.returncode != 0:
        raise CaptureError(f"command exited {p.returncode}: {' '.join(argv)}\n{out[-800:]}")
