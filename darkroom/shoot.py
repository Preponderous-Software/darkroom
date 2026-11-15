"""Shooting: capture each shot (with retries and a lock), keep the take, encode outputs,
and remember what was made so the next run can skip it (resume)."""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import __version__
from . import encode as enc
from . import procs
from .backends import command as command_backend
from .backends import web as web_backend
from .backends.command import CaptureError
from .locks import LockTimeout, lock, lock_holder
from .manifest import STILL_EXTS, Manifest, Shot, slug, template

BACKENDS = {"web": web_backend.capture, "command": command_backend.capture}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def say(msg: str) -> None:
    print(_ANSI.sub("", msg), flush=True)


def base_name(shot: Shot, tag: str | None) -> str:
    return f"{shot.name}.{tag}" if tag else shot.name


class State:
    """``.darkroom/state.json``. Several runs can shoot one manifest at once (``--only A`` and
    ``--only B``), so every update re-reads the file under an exclusive lock, changes its own
    entry and writes it back atomically (temp file and rename): no run loses another's entries."""

    def __init__(self, m: Manifest):
        self.path = m.state_dir / "state.json"
        self.data = self._read()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, entry):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _THREADS, _file_lock(self.path.with_name("state.lock")):
            self.data = self._read()
            self.data[key] = entry
            tmp = self.path.with_name(f".state.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
            tmp.replace(self.path)


@contextlib.contextmanager
def _file_lock(path: Path):
    """An exclusive flock on PATH for the block (released on exit, even on a crash)."""
    try:
        import fcntl
    except ImportError:  # Windows: no cross-process lock
        yield
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


_THREADS = threading.Lock()  # shoot --jobs: one state write at a time within this process too


def take_dir(m: Manifest, key: str) -> Path:
    return m.state_dir / "takes" / key


# State written by these versions is recognised (their whole-shot hash) instead of reshooting.
LEGACY_VERSIONS = ("0.2.0", "0.1.0")


def take_capture_hash(m: Manifest, key: str) -> str | None:
    """The capture hash recorded in a saved take's meta.json (None for no take, or one from 0.2)."""
    try:
        return json.loads((take_dir(m, key) / "meta.json").read_text()).get("capture_hash")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _stamp_take(m: Manifest, key: str, cap_h: str) -> None:
    path = take_dir(m, key) / "meta.json"
    try:
        meta = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    meta["capture_hash"] = cap_h
    path.write_text(json.dumps(meta, indent=2))


def output_base(m: Manifest, shot: Shot, tag: str | None, variables: dict | None = None) -> Path:
    """Where a shot's outputs go, without the suffix: ``<out>/<name>[.<tag>]``, or the shot's
    ``file`` (relative to the manifest; its extension stripped) with the tag before the extension."""
    f = shot.get("file")
    if not f:
        return m.out_dir / base_name(shot, tag)
    p = m.dir / template(f, {**m.shot_vars(shot), "name": shot.name, "tag": tag or "", **(variables or {})})
    stem = p.parent / p.name[: len(p.name) - len(p.suffix)] if p.suffix else p
    if m.out_override is not None:  # shoot --out: review copies, never the real files
        stem = m.out_dir / stem.name
    return stem.parent / (stem.name + (f".{tag}" if tag else ""))


def still_format(shot: Shot) -> tuple[str, str]:
    """(format, extension) for a still/pair/grid: ``file``'s extension wins over ``format``."""
    f = shot.get("file")
    if f and Path(f).suffix.lower() in STILL_EXTS:
        ext = Path(f).suffix.lower()
        return STILL_EXTS[ext], ext
    fmt = shot.get("format", "jpg")
    return fmt, f".{fmt}"


def _rel(m: Manifest, p: Path) -> str:
    return str(Path(p).resolve().relative_to(m.dir)) if Path(p).resolve().is_relative_to(m.dir) else str(p)


def device_label(m: Manifest, shot: Shot, variables: dict | None = None) -> str:
    """A device's name for logs and the gallery: its name, or WIDTHxHEIGHT for an inline one."""
    name = m.device_name(shot, variables)
    if name is not None:
        return name
    d = m.device(shot.get("device"))
    return f"{d['width']}x{d['height']}"


def console_types(shot: Shot) -> set[str]:
    """The console message types that fail an attempt (``failOnConsole``; ``failOnConsoleError``
    is the same as ``["error"]``)."""
    types = set(shot.get("failOnConsole") or [])
    if shot.get("failOnConsoleError"):
        types.add("error")
    return types


def _console_failures(console: list[dict], errs: list[str], types: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in console:
        t = "error" if c.get("type") == "pageerror" else c.get("type")
        if t in types:
            counts[t] = counts.get(t, 0) + 1
    if "error" in types and not console and errs:  # a result without the full log
        counts["error"] = len(errs)
    return counts


def capture_take(m: Manifest, shot: Shot, tag: str | None, retries: int, log=say, session=None) -> dict:
    """Run the backend (fresh browser / process each attempt) and save frames + meta.json.
    With a web ``session``, the steps run on the session's page; a failed attempt closes the
    session, so a retry starts with a fresh page."""
    key = base_name(shot, tag)
    tdir = take_dir(m, key)
    variables = dict(m.shot_vars(shot), tag=tag or "")
    try:
        if m.device_varies(shot):
            device_for = lambda v: m.shot_device(shot, v)  # noqa: E731
            device = device_for(m.run_vars(shot, 0))
            devices = [device_label(m, shot, m.run_vars(shot, i)) for i in range(len(shot["each"]))]
        else:
            device_for = None
            device = m.shot_device(shot, variables)
            devices = [device_label(m, shot, variables)]
    except KeyError as e:
        raise CaptureError(str(e.args[0] if e.args else e))
    attempts = 1 + retries
    fail_on = console_types(shot)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        frames_dir = tdir / "frames"
        if frames_dir.exists():
            shutil.rmtree(frames_dir)
        try:
            t0 = time.monotonic()
            if session is not None:
                # The session holds the shot's lock from opening the page until it closes.
                hold = lambda: lock(shot.get("lock"), timeout=shot.get("lockTimeout"),  # noqa: E731
                                    on_wait=_lock_wait(key, log))
                result = session.shot(shot, device, variables, frames_dir, log=log, hold_lock=hold)
                result["runs"] = [0] * len(result["frames"])
            else:
                with lock(shot.get("lock"), timeout=shot.get("lockTimeout"), on_wait=_lock_wait(key, log)):
                    kw = {"device_for": device_for} if device_for else {}
                    result = BACKENDS[shot.backend](shot, device, variables, frames_dir, log=log, **kw)
            if not result["frames"]:
                raise CaptureError("no frames captured")
            for warning in result.get("warnings") or []:
                log(f"  {key}: {warning}")
            errs = result.get("consoleErrors") or []
            console = result.get("console") or []
            for e in errs[:5]:
                log(f"  {key}: page error: {e[:200]}")
            if shot.backend == "web":
                tdir.mkdir(parents=True, exist_ok=True)
                with open(tdir / "console.jsonl", "w") as f:
                    for c in console:
                        f.write(json.dumps(c) + "\n")
                warns = sum(1 for c in console if c.get("type") == "warning")
                if warns:
                    log(f"  {key}: {warns} console warning(s) (see .darkroom/takes/{key}/console.jsonl)")
            bad = _console_failures(console, errs, fail_on)
            if bad:
                what = ", ".join(f"{n} console {t}(s)" for t, n in sorted(bad.items()))
                setting = "failOnConsole" if shot.get("failOnConsole") else "failOnConsoleError"
                raise CaptureError(f"{what} and {setting} is set")
            meta = {"shot": shot.name, "tag": tag, "kind": shot.kind, "backend": shot.backend,
                    "manifest": m.path.name, "out": m.data.get("out", "media"),
                    "device": device, "frames": [Path(f).name for f in result["frames"]],
                    "runs": result.get("runs") or [0] * len(result["frames"]),
                    "consoleErrors": errs, "seconds": round(time.monotonic() - t0, 1),
                    "attempt": attempt, "when": _dt.datetime.now().isoformat(timespec="seconds"),
                    "darkroom": __version__, "capture_hash": m.capture_hash(shot, tag)}
            meta["devices"] = devices
            if shot.backend == "web":
                counts: dict[str, int] = {}
                for c in console:
                    counts[c.get("type")] = counts.get(c.get("type"), 0) + 1
                meta["console"] = counts
            if session is not None:
                meta["session"] = session.name
            # A crop measured from an element at capture time: the rectangle used (CSS px),
            # kept with the take since the frames were cut to it.
            if result.get("crop"):
                meta["crop"] = result["crop"]
            elif result.get("crops"):
                meta["crops"] = result["crops"]
            if any(result.get("captions") or []):
                meta["captions"] = result["captions"]
            if any(result.get("names") or []):
                meta["names"] = result["names"]
            (tdir / "meta.json").write_text(json.dumps(meta, indent=2))
            return meta
        except (CaptureError, LockTimeout, OSError, ValueError) as e:
            last = e
            if session is not None:
                session.close()  # a failed shot discards the session; a retry starts fresh
            msg = [l for l in _ANSI.sub("", str(e)).strip().splitlines() if l.strip() and "Call log" not in l]
            log(f"  {key}: attempt {attempt}/{attempts} failed: {msg[0] if msg else e}")
            for extra in msg[1:4]:
                log(f"      {extra}")
    raise CaptureError(f"{key}: all {attempts} attempt(s) failed: {last}")


def strip_device(text: str, device: str | None) -> str:
    """A screen's row name: TEXT without the device's name ("title-phone" -> "title")."""
    if device:
        text = re.sub(rf"(^|[-_.]){re.escape(device)}(?=$|[-_.])", "", text)
    return text.strip("-_.")


def load_frames(m: Manifest, key: str, shot: Shot | None = None):
    tdir = take_dir(m, key)
    meta = json.loads((tdir / "meta.json").read_text())
    frames = [enc.load_rgb(tdir / "frames" / f) for f in meta["frames"]]
    if shot is not None and shot.get("aspect"):
        frames = [enc.crop_aspect(f, shot["aspect"]) for f in frames]
    return frames, meta


def last_of_each_run(meta: dict) -> list[int]:
    """Index of the last frame of every run, in run order."""
    runs = meta.get("runs") or [0] * len(meta["frames"])
    last: dict[int, int] = {}
    for k, r in enumerate(runs):
        last[r] = k
    return [last[r] for r in sorted(last)]


def sheet_options(shot: Shot) -> dict:
    sheet = shot.get("sheet")
    return sheet if isinstance(sheet, dict) else {}


def sheet_labels(mode: str, meta: dict) -> str | list:
    """``caption`` labels each frame with its ``each`` value (the index where there is none)."""
    if mode == "caption":
        return meta.get("captions") or "index"
    return mode


def encode_take(m: Manifest, shot: Shot, tag: str | None, log=say, cells: list | None = None) -> list[str]:
    """Encode a saved take into the output dir. Returns output paths relative to the manifest.
    ``cells`` (a list) gets one {"file", "row", "device"} per still, for the gallery's table."""
    key = base_name(shot, tag)
    frames, meta = load_frames(m, key, shot)
    runs = meta.get("runs") or [0] * len(frames)
    devices = meta.get("devices") or [device_label(m, shot)]
    row_default = (shot.get("gallery") or {}).get("row") if isinstance(shot.get("gallery"), dict) else None

    def cell(r: enc.Result, row: str, k: int) -> None:
        if cells is not None:
            dev = devices[runs[k]] if runs[k] < len(devices) else devices[0]
            cells.append({"file": _rel(m, r.path), "row": strip_device(str(row), dev) or row_default or shot.name,
                          "device": dev})
    budgets = m.budgets(shot)
    base = output_base(m, shot, tag)

    def at(suffix: str) -> Path:
        return base.parent / (base.name + suffix)

    results: list[enc.Result] = []
    colors = shot.get("colors")
    if shot.kind == "still":
        fmt, ext = still_format(shot)
        names = meta.get("names") or []
        named = shot.named_outputs()
        if named and shot.backend == "command":
            # A command still's named outputs: <name>-<key>.<ext>, or the `file` template with {key}.
            templated = "{key}" in (shot.get("file") or "")
            by_key = dict(zip(names, frames))
            for okey, _ in named:
                if okey not in by_key:
                    raise CaptureError(f"{key}: the saved take has no output '{okey}'; reshoot it")
                if templated:
                    b = output_base(m, shot, tag, {"key": okey})
                    path = b.parent / (b.name + ext)
                else:
                    path = at(f"-{okey}{ext}")
                results.append(enc.encode_still(by_key[okey], path, budgets["still_kb"], fmt,
                                                shot.get("quality", 86), shot.get("width"), colors))
                cell(results[-1], okey, names.index(okey))
        elif any(names):
            # Named captures: one still per name, beside the shot's other outputs.
            suffix = f".{tag}" if tag else ""
            if len([n for n in names if n]) != len({n for n in names if n}):
                raise CaptureError(f"{key}: the saved take names one capture twice; reshoot it")
            for k, name in enumerate(names):
                if name:
                    results.append(enc.encode_still(frames[k], base.parent / f"{name}{suffix}{ext}",
                                                    budgets["still_kb"], fmt, shot.get("quality", 86),
                                                    shot.get("width"), colors))
                    cell(results[-1], name, k)
        elif shot.get("each"):
            # One still per value: the last frame of each run.
            values = shot["each"]
            templated = any(t in (shot.get("file") or "") for t in ("{value}", "{i}"))
            for run, k in enumerate(last_of_each_run(meta)):
                value = values[run] if run < len(values) else run
                if templated:
                    b = output_base(m, shot, tag, {"value": value, "i": run})
                    path = b.parent / (b.name + ext)
                else:
                    path = at(f"-{slug(value)}{ext}")
                results.append(enc.encode_still(frames[k], path, budgets["still_kb"], fmt,
                                                shot.get("quality", 86), shot.get("width"), colors))
                # The row is the value, unless the value is the device ("device": "{value}").
                dev = devices[run] if run < len(devices) else devices[0]
                cell(results[-1], "" if str(value) == dev else value, k)
        else:
            results.append(enc.encode_still(frames[-1], at(ext), budgets["still_kb"], fmt,
                                            shot.get("quality", 86), shot.get("width"), colors))
            cell(results[-1], row_default or shot.name, len(frames) - 1)
    else:
        gif = shot.get("gif", {})
        fps = float(shot.get("fps", (gif or {}).get("fps", 12))) * float(shot.get("speed", 1))
        every = shot.get("every", 1)
        if gif is not False:
            g = gif or {}
            results.append(enc.encode_gif(frames[::g.get("every", every)], at(".gif"),
                                          g.get("budget_mb", budgets["gif_mb"]),
                                          fps=fps, width=g.get("width", shot.get("width", 640)),
                                          palette=g.get("palette", "adaptive"), colors=g.get("colors", 256),
                                          dither=g.get("dither", False), loop=g.get("loop", 0),
                                          hold_last_ms=g.get("hold_last_ms", 0)))
        vopts = video_options(shot)
        video = vopts["formats"]
        for fmt in video:
            r = enc.encode_video(frames[::vopts.get("every", every)], at(f".{fmt}"), fmt, budgets[f"{fmt}_kb"],
                                 fps=fps, width=shot.get("width"), hold_last=vopts.get("hold_last", 0))
            if r is None:
                log(f"  {key}: skipped {fmt}: no ffmpeg (install ffmpeg, or pip install 'darkroom[video]')")
            else:
                results.append(r)
        gallery_poster = isinstance(shot.get("gallery"), dict) and shot["gallery"].get("poster")
        poster = shot.get("poster", 0.6 if video or gallery_poster else None)
        if poster is not None and poster is not False:
            i = poster if isinstance(poster, int) and not isinstance(poster, bool) else round(float(poster) * (len(frames) - 1))
            i = max(0, min(len(frames) - 1, i))
            results.append(enc.encode_still(frames[i], at("-poster.jpg"), budgets["still_kb"],
                                            "jpg", shot.get("quality", 86), shot.get("width")))
        opts = sheet_options(shot)
        publish = bool(shot.get("sheet"))
        r = enc.contact_sheet(frames, at("-sheet.png") if publish else take_dir(m, key) / "sheet.png",
                              cols=opts.get("cols"), labels=sheet_labels(opts.get("labels", "index"), meta),
                              width=opts.get("width", enc.SHEET_WIDTH))
        if publish:
            results.append(r)
    for r in results:
        log(f"  {r.describe()}")
    return [_rel(m, r.path) for r in results]


def video_options(shot: Shot) -> dict:
    """``video`` as ``{"formats": [...], "every"?, "hold_last"?}`` (it may be a list or a string)."""
    v = shot.get("video", [])
    if isinstance(v, dict):
        out = dict(v)
        f = out.get("formats", [])
        out["formats"] = [f] if isinstance(f, str) else list(f)
        return out
    return {"formats": [v] if isinstance(v, str) else list(v)}


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
    fmt, ext = still_format(shot)
    base = output_base(m, shot, None)
    r = enc.encode_still(combo, base.parent / (base.name + ext), m.budgets(shot)["still_kb"], fmt,
                         shot.get("quality", 88), shot.get("width"), shot.get("colors"))
    log(f"  {r.describe()}")
    return [_rel(m, r.path)]


def _take_of(m: Manifest, name: str, for_shot: str):
    src = m.shot(name)
    try:
        frames, meta = load_frames(m, base_name(src, None), src)
    except FileNotFoundError:
        raise CaptureError(f"grid '{for_shot}': no take of '{name}' yet; run: darkroom shoot <manifest> --only {name}")
    return src, frames, meta


def make_grid(m: Manifest, shot: Shot, log=say) -> list[str]:
    """A captioned composite (no frame numbers) of another shot's frames or of several shots."""
    images, captions = [], []
    if "of" in shot.raw:
        src, frames, meta = _take_of(m, shot["of"], shot.name)
        picks = shot.get("frames")
        if picks is None:
            # One tile per run of an `each`/`repeat` shot; otherwise every frame.
            picks = last_of_each_run(meta) if len(set(meta.get("runs") or [0])) > 1 else list(range(len(frames)))
        frame_caps = meta.get("captions") or []
        for i in picks:
            if not -len(frames) <= i < len(frames):
                raise CaptureError(f"grid '{shot.name}': frame {i} is out of range; "
                                   f"'{src.name}' has {len(frames)} frame(s)")
            images.append(frames[i])
            captions.append(frame_caps[i] if frame_caps else None)
    else:
        for t in shot["tiles"]:
            t = {"shot": t} if isinstance(t, str) else t
            if "image" in t:
                images.append(enc.load_rgb(m.dir / t["image"]))
                captions.append(t.get("caption"))
                continue
            src, frames, meta = _take_of(m, t["shot"], shot.name)
            i = t.get("frame", -1)
            if not -len(frames) <= i < len(frames):
                raise CaptureError(f"grid '{shot.name}': frame {i} of '{src.name}' is out of range "
                                   f"({len(frames)} frame(s))")
            caps = meta.get("captions") or []
            captions.append(t.get("caption") or (caps[i] if caps else None) or src.get("caption"))
            images.append(frames[i])
    if shot.get("captions") is not None:
        given = shot["captions"]
        captions = [given[k] if k < len(given) else captions[k] for k in range(len(images))]
    if shot.get("aspect"):
        images = [enc.crop_aspect(i, shot["aspect"]) for i in images]
    combo = enc.grid(images, captions, shot.get("title"), shot.get("cols"))
    fmt, ext = still_format(shot)
    base = output_base(m, shot, None)
    r = enc.encode_still(combo, base.parent / (base.name + ext), m.budgets(shot)["still_kb"], fmt,
                         shot.get("quality", 88), shot.get("width"), shot.get("colors"))
    log(f"  {r.describe()}")
    return [_rel(m, r.path)]


def plan(m: Manifest, only=None) -> list[Shot]:
    """The shots to run, in order. With ``only``, the preps the named shots depend on (directly
    or through other preps) are added; they still skip themselves when up to date. Shots run in
    manifest order, pairs and grids last (they read other shots' takes), except that every shot
    runs after the shots it ``depends`` on."""
    all_shots = {s.name: s for s in m.shots if not s.get("skip")}
    chosen = [s for s in all_shots.values() if not only or s.name in only]
    if only:
        names = {s.name for s in chosen}
        stack = list(chosen)
        while stack:
            for dep in stack.pop().get("depends") or []:
                d = all_shots.get(dep)
                if d is not None and d.prep and d.name not in names:
                    names.add(d.name)
                    chosen.append(d)
                    stack.append(d)
    order = sorted(chosen, key=lambda s: (s.composite, s.index))
    names = {s.name for s in order}
    out: list[Shot] = []
    placed: set[str] = set()
    while order:
        # The first shot (in the usual order) whose dependencies (in this run) have all run.
        for k, s in enumerate(order):
            if all(d in placed or d not in names for d in s.get("depends") or []):
                break
        else:  # a cycle; validation rejects these, so keep the usual order
            k = 0
        s = order.pop(k)
        out.append(s)
        placed.add(s.name)
    return out


def run_prep(m: Manifest, shot: Shot, state: State, retries: int, force: bool, log=say) -> None:
    """Run a prep unless it is up to date: same capture hash (argv, env, inputs, vars...) and
    every declared output still exists."""
    key = shot.name
    cap_h = m.capture_hash(shot, None)
    outputs = [str(o) for o in template(shot["outputs"], m.shot_vars(shot))]
    prev = state.get(key) or {}
    if not force and prev.get("capture_hash") == cap_h and all((m.dir / o).exists() for o in outputs):
        log(f"= {key}: up to date (use --force to rerun)")
        return
    log(f"> {key} (prep)")
    device = m.device(shot.get("device", "desktop"))
    attempts = 1 + retries
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            t0 = time.monotonic()
            with lock(shot.get("lock"), timeout=shot.get("lockTimeout"), on_wait=_lock_wait(key, log)):
                command_backend.run_prep(shot, device, dict(m.shot_vars(shot), tag=""), log=log)
            absent = [o for o in outputs if not (m.dir / o).exists()]
            if absent:
                raise CaptureError(f"the prep did not write {', '.join(absent)} (its declared outputs)")
            for o in outputs:
                log(f"  {o}")
            state.put(key, {"capture_hash": cap_h, "outputs": outputs, "kind": "prep", "darkroom": __version__,
                            "seconds": round(time.monotonic() - t0, 1),
                            "when": _dt.datetime.now().isoformat(timespec="seconds")})
            return
        except (CaptureError, LockTimeout, OSError, ValueError) as e:
            last = e
            msg = [l for l in _ANSI.sub("", str(e)).strip().splitlines() if l.strip()]
            log(f"  {key}: attempt {attempt}/{attempts} failed: {msg[0] if msg else e}")
            for extra in msg[1:4]:
                log(f"      {extra}")
    raise CaptureError(f"{key}: all {attempts} attempt(s) failed: {last}")


def _lock_wait(key: str, log):
    def waiting(path):
        holder = lock_holder(path)
        log(f"  {key}: waiting for lock {path}" + (f" (held by pid {holder})" if holder else ""))
    return waiting


def units(order: list[Shot]) -> list[list[Shot]]:
    """Group the plan into units of work: a run of consecutive web shots with the same
    ``session`` is one unit (they share a page, in order); every other shot is its own."""
    out: list[list[Shot]] = []
    for shot in order:
        name = shot.get("session") if shot.backend == "web" and not shot.composite else None
        if name and out and out[-1][0].get("session") == name and out[-1][0].backend == "web":
            out[-1].append(shot)
        else:
            out.append([shot])
    return out


class _Shooter:
    """One ``shoot``: runs units of work in plan order, or with ``jobs`` > 1, several at once.
    A unit starts once the shots it depends on have finished, never beside a running unit that
    uses one of its ``lock`` names, and pairs and grids start after every other shot."""

    def __init__(self, m: Manifest, state: State, only, tag, retries, force, encode_only, log, jobs: int):
        import threading
        self.m, self.state, self.only, self.tag = m, state, only, tag
        self.retries, self.force, self.encode_only = retries, force, encode_only
        self.jobs = max(1, int(jobs or 1))
        self._log = log
        self.failed = 0
        self.broken: set[str] = set()  # shots that failed (or were skipped for it) in this run
        self.mutex = threading.Lock()
        self._print = threading.Lock()

    def logger(self, key: str):
        """With jobs > 1 every line a shot logs starts with ``[key]``."""
        if self.jobs == 1:
            return self._log

        def log(msg: str) -> None:
            with self._print:
                self._log(f"[{key}] {msg}")
        return log

    def fail(self, name: str) -> None:
        with self.mutex:
            self.failed += 1
            self.broken.add(name)

    def run_unit(self, unit: list[Shot]) -> None:
        session = None
        if unit[0].get("session") and unit[0].backend == "web" and not unit[0].composite:
            from .backends.web import WebSession
            session = WebSession(unit[0]["session"], self.logger(f"session {unit[0]['session']}"))
        try:
            for shot in unit:
                self.run_shot(shot, session)
        finally:
            if session is not None:
                session.close()

    def run_shot(self, shot: Shot, session=None) -> None:
        m, state, tag, only = self.m, self.state, self.tag, self.only
        key = base_name(shot, None if shot.prep else tag)
        log = self.logger(key)
        with self.mutex:
            bad = [d for d in shot.get("depends") or [] if d in self.broken]
        if bad:
            self.fail(shot.name)
            log(f"! {key}: skipped: depends on " + ", ".join(f"'{d}'" for d in bad) + ", which failed")
            return
        retries = shot.get("retries", 2) if self.retries is None else self.retries
        if shot.prep:
            if self.encode_only:
                return  # a prep makes no images, so there is nothing to re-encode
            with procs.shot_scope(shot.name):
                try:
                    run_prep(m, shot, state, retries, self.force and (not only or shot.name in only), log)
                except Exception as e:
                    self.fail(shot.name)
                    log(f"! {shot.name}: FAILED: {str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__}")
            return
        # A review shoot (--out) keeps its own record, so the real outputs' entry is untouched.
        skey = key if m.out_override is None else f"{key}@{m.out_dir}"
        try:
            cap_h, enc_h = m.capture_hash(shot, tag), m.encode_hash(shot, tag)
        except KeyError as e:  # e.g. --var gave "device": "{dev}" a name that is not a device
            self.fail(shot.name)
            log(f"! {key}: FAILED: {e.args[0] if e.args else e}")
            return
        prev = state.get(skey) or {}
        outputs_ok = bool(prev) and all((m.dir / o).exists() for o in prev.get("outputs", []))
        if (prev.get("adopted") and not self.force and not self.encode_only and outputs_ok
                and prev.get("capture_hash") == cap_h and prev.get("encode_hash") == enc_h):
            # Recorded by `shoot --adopt` (outputs committed, no take here): up to date until it changes.
            log(f"= {key}: up to date (adopted; use --force to reshoot)")
            return
        reencode = False
        if not shot.composite and not self.force and not self.encode_only:
            take = take_capture_hash(m, key)
            if take is None and prev.get("hash") and "capture_hash" not in prev and outputs_ok and any(
                    prev["hash"] == m.legacy_hash(shot, tag, v) for v in LEGACY_VERSIONS):
                # State from darkroom 0.1/0.2 for this exact shot: adopt it instead of reshooting.
                state.put(skey, dict(prev, capture_hash=cap_h, encode_hash=enc_h, darkroom=__version__))
                _stamp_take(m, key, cap_h)
                log(f"= {key}: up to date (use --force to reshoot)")
                return
            if take == cap_h:
                if prev.get("encode_hash") == enc_h and outputs_ok:
                    log(f"= {key}: up to date (use --force to reshoot)")
                    return
                reencode = True
        with procs.shot_scope(key):
            log(f"> {key} ({shot.kind}{'' if shot.composite else ', ' + shot.backend})"
                + (": capture unchanged, re-encoding the saved take" if reencode else ""))
            try:
                cells: list[dict] = []
                if shot.kind == "pair":
                    outputs = make_pair(m, shot, log)
                elif shot.kind == "grid":
                    outputs = make_grid(m, shot, log)
                else:
                    if not (self.encode_only or reencode):
                        capture_take(m, shot, tag, retries, log, session=session)
                    elif not (take_dir(m, key) / "meta.json").exists():
                        raise CaptureError(f"{key}: no saved take to re-encode; shoot it first")
                    outputs = encode_take(m, shot, tag, log, cells=cells)
                entry = {"hash": m.shot_hash(shot, tag), "capture_hash": cap_h, "encode_hash": enc_h,
                         "outputs": outputs, "tag": tag, "darkroom": __version__,
                         "when": _dt.datetime.now().isoformat(timespec="seconds")}
                if cells:
                    entry["cells"] = cells
                state.put(skey, entry)
            except Exception as e:  # one failing shot doesn't stop the others
                self.fail(shot.name)
                log(f"! {key}: FAILED: {str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__}")

    def run(self, work: list[list[Shot]]) -> None:
        if self.jobs == 1:
            for unit in work:
                self.run_unit(unit)
            return
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
        in_run = {s.name for u in work for s in u}
        pending = list(work)
        running: dict = {}
        finished: set[str] = set()

        def locks(u):
            return {s.get("lock") for s in u if s.get("lock")}

        def ready(u) -> bool:
            names = {s.name for s in u}
            deps = {d for s in u for d in s.get("depends") or []} - names
            if any(d in in_run and d not in finished for d in deps):
                return False
            if u[0].composite:  # pairs and grids read other shots' takes: they go last
                return not any(not v[0].composite for v in pending) and \
                    not any(not v[0].composite for v in running.values())
            return True

        pool = ThreadPoolExecutor(max_workers=self.jobs, thread_name_prefix="darkroom-shot")
        try:
            while pending or running:
                busy = set().union(*(locks(u) for u in running.values()))
                for u in list(pending):
                    if len(running) >= self.jobs:
                        break
                    if ready(u) and not (locks(u) & busy):
                        pending.remove(u)
                        running[pool.submit(self.run_unit, u)] = u
                        busy |= locks(u)
                if not running:  # cannot happen with a valid plan; keep going in order
                    u = pending.pop(0)
                    running[pool.submit(self.run_unit, u)] = u
                done, _ = wait(list(running), timeout=0.5, return_when=FIRST_COMPLETED)
                for f in done:
                    u = running.pop(f)
                    finished |= {s.name for s in u}
                    f.result()  # an unexpected error (not a shot failure) stops the run
        except BaseException:
            # Ctrl-C, a stop or an unexpected error: stop starting processes, kill the ones the
            # workers are waiting on, then let the workers finish unwinding.
            run = procs.current()
            if run is not None:
                run.close_down()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown(wait=True)


def shoot(m: Manifest, only=None, tag=None, retries=None, force=False, encode_only=False, log=say,
          jobs: int = 1) -> int:
    """Shoot (or with encode_only, re-encode the last takes of) the selected shots, ``jobs`` at
    a time. Returns the number of failed shots."""
    state = State(m)
    if only:
        present = {s.name for s in m.shots if not s.get("skip")}
        missing = set(only) - present
        for name in sorted(missing):
            log(f"no shot named '{name}'")
        if missing:
            return len(missing)
    order = [s for s in plan(m, only) if not (s.composite and tag)]
    shooter = _Shooter(m, state, only, tag, retries, force, encode_only, log, jobs)
    shooter.run(units(order))
    return shooter.failed


def expected_outputs(m: Manifest, shot: Shot, tag: str | None = None) -> list[Path]:
    """The files a shot makes, worked out from the manifest alone (no take): what ``--adopt``
    checks before recording a shot as up to date."""
    from .backends.web import template_steps
    from .manifest import _named_captures
    base = output_base(m, shot, tag)

    def at(suffix: str) -> Path:
        return base.parent / (base.name + suffix)
    if shot.kind in ("still", "pair", "grid"):
        fmt, ext = still_format(shot)
        if shot.kind != "still":
            return [at(ext)]
        named = shot.named_outputs()
        if named and shot.backend == "command":
            templated = "{key}" in (shot.get("file") or "")
            return [(lambda b: b.parent / (b.name + ext))(output_base(m, shot, tag, {"key": k})) if templated
                    else at(f"-{k}{ext}") for k, _ in named]
        suffix = f".{tag}" if tag else ""
        each = shot.get("each") or []
        if shot.backend == "web" and _named_captures(shot.raw):
            out = []
            for i, value in enumerate(each or [None]):
                v = dict(m.shot_vars(shot), **({"value": value, "i": i, "n": len(each)} if each else {}))
                steps = template_steps((shot.get("setup") or []) + (shot.get("steps") or []), v)
                names = _named_captures({"steps": steps})
                out += [base.parent / f"{n}{suffix}{ext}" for n in names]
            return out
        if each:
            templated = any(t in (shot.get("file") or "") for t in ("{value}", "{i}"))
            return [(lambda b: b.parent / (b.name + ext))(output_base(m, shot, tag, {"value": v, "i": i}))
                    if templated else at(f"-{slug(v)}{ext}") for i, v in enumerate(each)]
        return [at(ext)]
    out = []
    if shot.get("gif", {}) is not False:
        out.append(at(".gif"))
    video = video_options(shot)["formats"]
    out += [at(f".{f}") for f in video]
    gallery_poster = isinstance(shot.get("gallery"), dict) and shot["gallery"].get("poster")
    poster = shot.get("poster", 0.6 if video or gallery_poster else None)
    if poster is not None and poster is not False:
        out.append(at("-poster.jpg"))
    if shot.get("sheet"):
        out.append(at("-sheet.png"))
    return out


def adopt(m: Manifest, only=None, tag=None, log=say) -> int:
    """Record every selected shot whose outputs all exist as up to date for its current capture
    and encode hashes, without shooting (a fresh checkout or worktree has no ``.darkroom``
    state, so committed outputs would otherwise all be reshot). Returns how many were adopted."""
    state = State(m)
    n = 0
    for shot in m.shots:
        if shot.get("skip") or shot.prep or (only and shot.name not in only) or (shot.composite and tag):
            continue
        key = base_name(shot, tag)
        skey = key if m.out_override is None else f"{key}@{m.out_dir}"
        try:
            files = expected_outputs(m, shot, tag)
            cap_h, enc_h = m.capture_hash(shot, tag), m.encode_hash(shot, tag)
        except (KeyError, ValueError) as e:
            log(f"! {key}: not adopted: {e}")
            continue
        missing = [f for f in files if not f.exists()]
        if not files or missing:
            log(f"- {key}: not adopted: " + (f"missing {_rel(m, missing[0])}" + (
                f" and {len(missing) - 1} more" if len(missing) > 1 else "") if missing else "no outputs"))
            continue
        state.put(skey, {"hash": m.shot_hash(shot, tag), "capture_hash": cap_h, "encode_hash": enc_h,
                         "outputs": [_rel(m, f) for f in files], "tag": tag, "darkroom": __version__,
                         "adopted": True, "when": _dt.datetime.now().isoformat(timespec="seconds")})
        log(f"= {key}: adopted ({len(files)} output(s))")
        n += 1
    log(f"adopted {n} shot(s); shoot reshoots them only when they change")
    return n
