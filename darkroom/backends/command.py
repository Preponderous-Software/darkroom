"""The command backend: run any program that writes images into a directory.

Each capture gets its own temporary ``{out_dir}`` and, with ``xvfb: true``, its own Xvfb
display (picked by Xvfb itself via ``-displayfd``), so parallel captures never collide.
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

from ..manifest import template

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
    """Run the shot's argv (``repeat`` times, with {i}/{n}); copy every image it writes into
    ``frames_dir`` in order. Returns {"frames": [...], "logs": [...]}."""
    repeat = int(shot.get("repeat", 1))
    timeout = float(shot.get("timeout", 600))
    frames: list[str] = []
    logs: list[str] = []
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i in range(repeat):
        with tempfile.TemporaryDirectory(prefix=f"darkroom-{shot.name}-") as tmp:
            v = dict(variables)
            v.update({"out_dir": tmp, "width": device["width"], "height": device["height"],
                      "dpr": device.get("dpr", 1), "name": shot.name, "i": i, "n": repeat})
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
            got = _collect(Path(tmp), shot.get("glob"))
            if not got:
                raise CaptureError(f"run {i + 1}/{repeat}: the command wrote no images into {{out_dir}}"
                                   + (f"; last output: {logs[-1][-400:]}" if logs else ""))
            for f in got:
                dest = frames_dir / f"{len(frames):05d}{f.suffix.lower()}"
                shutil.copyfile(f, dest)
                frames.append(str(dest))
    return {"frames": frames, "logs": logs, "consoleErrors": []}


def _run(shot, v, timeout, logs, display, gl_env=None):
    argv = template(shot["argv"], v)
    env = dict(os.environ)
    env.update(gl_env or {})  # the same GL as the display's (the shot's own env still wins)
    env.update(template(shot.get("env", {}), v))
    if display:
        env["DISPLAY"] = display
    cwd = template(shot.get("cwd", v.get("manifest_dir", ".")), v)
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
