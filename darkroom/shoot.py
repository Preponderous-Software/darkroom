"""Shooting: capture each shot (with retries and a lock), keep the take, encode outputs,
and remember what was made so the next run can skip it (resume)."""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import time
from pathlib import Path

from . import encode as enc
from .backends import command as command_backend
from .backends import web as web_backend
from .backends.command import CaptureError
from .locks import lock
from .manifest import Manifest, Shot

BACKENDS = {"web": web_backend.capture, "command": command_backend.capture}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def say(msg: str) -> None:
    print(_ANSI.sub("", msg), flush=True)


def base_name(shot: Shot, tag: str | None) -> str:
    return f"{shot.name}.{tag}" if tag else shot.name


class State:
    def __init__(self, m: Manifest):
        self.path = m.state_dir / "state.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, entry):
        self.data[key] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)


def take_dir(m: Manifest, key: str) -> Path:
    return m.state_dir / "takes" / key


def _rel(m: Manifest, p: Path) -> str:
    return str(Path(p).resolve().relative_to(m.dir)) if Path(p).resolve().is_relative_to(m.dir) else str(p)


def capture_take(m: Manifest, shot: Shot, tag: str | None, retries: int, log=say) -> dict:
    """Run the backend (fresh browser / process each attempt) and save frames + meta.json."""
    key = base_name(shot, tag)
    tdir = take_dir(m, key)
    device = m.device(shot.get("device", "desktop"))
    variables = dict(m.vars, tag=tag or "")
    attempts = 1 + retries
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        frames_dir = tdir / "frames"
        if frames_dir.exists():
            shutil.rmtree(frames_dir)
        try:
            t0 = time.monotonic()
            with lock(shot.get("lock"), on_wait=lambda p: log(f"  {key}: waiting for lock {p}")):
                result = BACKENDS[shot.backend](shot, device, variables, frames_dir, log=log)
            if not result["frames"]:
                raise CaptureError("no frames captured")
            errs = result.get("consoleErrors") or []
            for e in errs[:5]:
                log(f"  {key}: page error: {e[:200]}")
            if errs and shot.get("failOnConsoleError"):
                raise CaptureError(f"{len(errs)} console error(s) and failOnConsoleError is set")
            meta = {"shot": shot.name, "tag": tag, "kind": shot.kind, "backend": shot.backend,
                    "device": device, "frames": [Path(f).name for f in result["frames"]],
                    "consoleErrors": errs, "seconds": round(time.monotonic() - t0, 1),
                    "attempt": attempt, "when": _dt.datetime.now().isoformat(timespec="seconds")}
            (tdir / "meta.json").write_text(json.dumps(meta, indent=2))
            return meta
        except (CaptureError, OSError, ValueError) as e:
            last = e
            msg = [l for l in _ANSI.sub("", str(e)).strip().splitlines() if l.strip() and "Call log" not in l]
            log(f"  {key}: attempt {attempt}/{attempts} failed: {msg[0] if msg else e}")
            for extra in msg[1:4]:
                log(f"      {extra}")
    raise CaptureError(f"{key}: all {attempts} attempt(s) failed: {last}")


def load_frames(m: Manifest, key: str, shot: Shot | None = None):
    tdir = take_dir(m, key)
    meta = json.loads((tdir / "meta.json").read_text())
    frames = [enc.load_rgb(tdir / "frames" / f) for f in meta["frames"]]
    if shot is not None and shot.get("aspect"):
        frames = [enc.crop_aspect(f, shot["aspect"]) for f in frames]
    return frames, meta


def encode_take(m: Manifest, shot: Shot, tag: str | None, log=say) -> list[str]:
    """Encode a saved take into the output dir. Returns output paths relative to the manifest."""
    key = base_name(shot, tag)
    frames, meta = load_frames(m, key, shot)
    budgets = m.budgets(shot)
    out = m.out_dir
    results: list[enc.Result] = []
    if shot.kind == "still":
        fmt = shot.get("format", "jpg")
        results.append(enc.encode_still(frames[-1], out / f"{key}.{fmt}", budgets["still_kb"], fmt,
                                         shot.get("quality", 86), shot.get("width")))
    else:
        gif = shot.get("gif", {})
        fps = float(shot.get("fps", (gif or {}).get("fps", 12))) * float(shot.get("speed", 1))
        if gif is not False:
            g = gif or {}
            results.append(enc.encode_gif(frames, out / f"{key}.gif", g.get("budget_mb", budgets["gif_mb"]),
                                          fps=fps, width=g.get("width", shot.get("width", 640)),
                                          palette=g.get("palette", "octree"), colors=g.get("colors", 256),
                                          dither=g.get("dither", False), loop=g.get("loop", 0)))
        video = shot.get("video", [])
        video = [video] if isinstance(video, str) else video
        for fmt in video:
            r = enc.encode_video(frames, out / f"{key}.{fmt}", fmt, budgets[f"{fmt}_kb"], fps=fps,
                                 width=shot.get("width"))
            if r is None:
                log(f"  {key}: skipped {fmt}: no ffmpeg (install ffmpeg, or pip install 'darkroom[video]')")
            else:
                results.append(r)
        poster = shot.get("poster", 0.6 if video else None)
        if poster is not None and poster is not False:
            i = poster if isinstance(poster, int) and not isinstance(poster, bool) else round(float(poster) * (len(frames) - 1))
            i = max(0, min(len(frames) - 1, i))
            results.append(enc.encode_still(frames[i], out / f"{key}-poster.jpg", budgets["still_kb"],
                                            "jpg", shot.get("quality", 86), shot.get("width")))
        sheet_path = out / f"{key}-sheet.png" if shot.get("sheet") else take_dir(m, key) / "sheet.png"
        r = enc.contact_sheet(frames, sheet_path)
        if shot.get("sheet"):
            results.append(r)
    for r in results:
        log(f"  {r.describe()}")
    return [_rel(m, r.path) for r in results]


def make_pair(m: Manifest, shot: Shot, log=say) -> list[str]:
    labels = shot.get("labels", ["before", "after"])
    if "of" in shot.raw:
        src = m.shot(shot["of"])
        imgs = []
        for tag in ("before", "after"):
            try:
                frames, _ = load_frames(m, base_name(src, tag), src)
            except FileNotFoundError:
                raise CaptureError(f"pair '{shot.name}': no '{tag}' take of '{src.name}' yet; "
                                   f"run: darkroom shoot <manifest> --only {src.name} --tag {tag}")
            imgs.append(frames[-1])
    else:
        imgs = [enc.load_rgb(m.dir / shot["before"]), enc.load_rgb(m.dir / shot["after"])]
    if shot.get("aspect"):
        imgs = [enc.crop_aspect(i, shot["aspect"]) for i in imgs]
    combo = enc.side_by_side(imgs[0], imgs[1], labels)
    fmt = shot.get("format", "jpg")
    r = enc.encode_still(combo, m.out_dir / f"{shot.name}.{fmt}", m.budgets(shot)["still_kb"], fmt,
                         shot.get("quality", 88), shot.get("width"))
    log(f"  {r.describe()}")
    return [_rel(m, r.path)]


def shoot(m: Manifest, only=None, tag=None, retries=None, force=False, encode_only=False, log=say) -> int:
    """Shoot (or with encode_only, re-encode the last takes of) the selected shots.
    Returns the number of failed shots."""
    state = State(m)
    shots = [s for s in m.shots if (not only or s.name in only) and not s.get("skip")]
    if only:
        missing = set(only) - {s.name for s in shots}
        for name in sorted(missing):
            log(f"no shot named '{name}'")
        if missing:
            return len(missing)
    failed = 0
    # Pairs last: they read other shots' takes.
    for shot in sorted(shots, key=lambda s: s.kind == "pair"):
        if shot.kind == "pair" and tag:
            continue
        key = base_name(shot, tag)
        h = m.shot_hash(shot, tag)
        prev = state.get(key)
        if (shot.kind != "pair" and not force and not encode_only and prev and prev.get("hash") == h
                and all((m.dir / o).exists() for o in prev.get("outputs", []))):
            log(f"= {key}: up to date (use --force to reshoot)")
            continue
        log(f"> {key} ({shot.kind}{'' if shot.kind == 'pair' else ', ' + shot.backend})")
        try:
            if shot.kind == "pair":
                outputs = make_pair(m, shot, log)
            else:
                if not encode_only:
                    capture_take(m, shot, tag, shot.get("retries", 2) if retries is None else retries, log)
                elif not (take_dir(m, key) / "meta.json").exists():
                    raise CaptureError(f"{key}: no saved take to re-encode; shoot it first")
                outputs = encode_take(m, shot, tag, log)
            state.put(key, {"hash": h, "outputs": outputs, "tag": tag,
                            "when": _dt.datetime.now().isoformat(timespec="seconds")})
        except Exception as e:  # one failing shot doesn't stop the others
            failed += 1
            log(f"! {key}: FAILED: {str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__}")
    return failed
