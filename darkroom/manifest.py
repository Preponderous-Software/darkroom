"""Manifest loading, validation and templating.

A manifest is a JSON file (conventionally ``media.json``). Validation is hand-written so
that every error names the exact place it happened, e.g. ``shots[2].steps[4]``.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEVICES: dict[str, dict[str, Any]] = {
    "desktop": {"width": 1280, "height": 720, "dpr": 1, "touch": False, "mobile": False},
    "hd": {"width": 1920, "height": 1080, "dpr": 1, "touch": False, "mobile": False},
    "phone": {"width": 390, "height": 844, "dpr": 3, "touch": True, "mobile": True},
    "landscape-phone": {"width": 844, "height": 390, "dpr": 3, "touch": True, "mobile": True},
}

DEFAULT_BUDGETS = {"still_kb": 200, "gif_mb": 3.0, "mp4_kb": 600, "webm_kb": 400}

KINDS = ("still", "clip", "pair")
BACKENDS = ("web", "command")
ADAPTERS = ("strata-kit", "hook")
PALETTES = ("octree", "median", "adaptive")
VIDEO_FORMATS = ("mp4", "webm")

TOP_KEYS = {"$schema", "out", "vars", "devices", "budgets", "defaults", "shots", "description"}
COMMON_SHOT_KEYS = {
    "name", "kind", "backend", "caption", "alt", "device", "retries", "timeout", "lock",
    "format", "quality", "width", "gif", "video", "poster", "sheet", "budgets", "skip", "comment",
    "aspect", "speed", "fps",
}
WEB_KEYS = {"url", "adapter", "ready", "readyTimeout", "css", "setup", "steps",
            "failOnConsoleError", "waitUntil", "launchArgs", "crop", "userAgent", "block"}
COMMAND_KEYS = {"argv", "env", "cwd", "xvfb", "repeat", "glob"}
PAIR_KEYS = {"of", "before", "after", "labels"}
GIF_KEYS = {"fps", "width", "palette", "colors", "dither", "loop", "budget_mb"}
DEVICE_KEYS = {"width", "height", "dpr", "touch", "mobile"}

STEP_KEYS = {
    "js": set(), "key": {"action"}, "tap": set(), "click": set(), "wait": set(),
    "waitFor": {"timeout"}, "frames": set(), "camera": set(), "capture": set(),
    "repeat": {"steps", "var"}, "css": set(), "type": set(), "mark": set(),
}
CAMERA_KEYS = {"yaw", "pitch", "dist", "tx", "ty", "tz"}

_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Placeholders the backends fill in themselves (always allowed in templates).
BUILTIN_VARS = {"out_dir", "width", "height", "dpr", "display", "name", "tag", "i", "n",
                "manifest_dir"}


class ManifestError(Exception):
    """Raised with a list of human-readable problems."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def template(value: Any, variables: dict[str, Any]) -> Any:
    """Replace ``{name}`` with ``variables[name]`` in strings (recursively in lists/dicts).

    Unknown placeholders are left untouched so literal braces (JSON, JS) survive.
    """
    if isinstance(value, str):
        return _VAR.sub(lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0), value)
    if isinstance(value, list):
        return [template(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: template(v, variables) for k, v in value.items()}
    return value


def placeholders(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_VAR.findall(value))
    if isinstance(value, list):
        return set().union(*[placeholders(v) for v in value]) if value else set()
    if isinstance(value, dict):
        return set().union(*[placeholders(v) for v in value.values()]) if value else set()
    return set()


def _suggest(word: str, options) -> str:
    close = difflib.get_close_matches(word, list(options), n=1)
    return f" (did you mean '{close[0]}'?)" if close else ""


@dataclass
class Shot:
    raw: dict[str, Any]
    index: int

    def __getitem__(self, k):
        return self.raw[k]

    def get(self, k, default=None):
        return self.raw.get(k, default)

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def kind(self) -> str:
        return self.raw.get("kind", "still")

    @property
    def backend(self) -> str | None:
        return None if self.kind == "pair" else self.raw.get("backend", "web")


@dataclass
class Manifest:
    path: Path
    data: dict[str, Any]
    vars: dict[str, Any] = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return self.path.parent

    @property
    def out_dir(self) -> Path:
        return (self.dir / self.data.get("out", "media")).resolve()

    @property
    def state_dir(self) -> Path:
        return self.dir / ".darkroom"

    def budgets(self, shot: Shot | None = None) -> dict[str, float]:
        b = dict(DEFAULT_BUDGETS)
        b.update(self.data.get("budgets", {}))
        if shot is not None:
            b.update(shot.get("budgets", {}))
        return b

    def device(self, name_or_spec) -> dict[str, Any]:
        if isinstance(name_or_spec, dict):
            d = {"dpr": 1, "touch": False, "mobile": False}
            d.update(name_or_spec)
            return d
        custom = self.data.get("devices", {})
        if name_or_spec in custom:
            d = {"dpr": 1, "touch": False, "mobile": False}
            d.update(custom[name_or_spec])
            return d
        return dict(DEVICES[name_or_spec])

    @property
    def shots(self) -> list[Shot]:
        defaults = self.data.get("defaults", {})
        out = []
        for i, s in enumerate(self.data.get("shots", [])):
            merged = copy.deepcopy(defaults)
            for k, v in s.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            # Defaults that belong to another backend (or to no backend, for pairs) are dropped.
            if merged.get("kind") == "pair":
                foreign = WEB_KEYS | COMMAND_KEYS | {"backend", "device"}
            elif merged.get("backend", "web") == "command":
                foreign = WEB_KEYS - COMMAND_KEYS
            else:
                foreign = COMMAND_KEYS - WEB_KEYS
            for k in list(merged):
                if k in foreign and k not in s:
                    del merged[k]
            out.append(Shot(merged, i))
        return out

    def shot(self, name: str) -> Shot:
        for s in self.shots:
            if s.name == name:
                return s
        raise KeyError(f"no shot named '{name}'" + _suggest(name, [s.name for s in self.shots]))

    def shot_hash(self, shot: Shot, tag: str | None) -> str:
        from . import __version__
        blob = json.dumps({"shot": shot.raw, "vars": self.vars, "tag": tag,
                           "budgets": self.budgets(shot), "version": __version__,
                           "device": self.device(shot.get("device", "desktop")) if shot.kind != "pair" else None},
                          sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load(path: str | Path, overrides: dict[str, str] | None = None) -> Manifest:
    path = Path(path).resolve()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ManifestError([f"{path}: file not found"])
    except json.JSONDecodeError as e:
        raise ManifestError([f"{path}:{e.lineno}:{e.colno}: invalid JSON: {e.msg}"])
    problems = validate(data)
    if problems:
        raise ManifestError(problems)
    variables = dict(data.get("vars", {}))
    variables.update(overrides or {})
    variables.setdefault("manifest_dir", str(path.parent))
    # Vars may refer to other vars ("page": "file://{manifest_dir}/index.html").
    for _ in range(5):
        expanded = {k: template(v, variables) for k, v in variables.items()}
        if expanded == variables:
            break
        variables = expanded
    return Manifest(path, data, variables)


def _check_keys(obj: dict, allowed: set, where: str, problems: list[str]):
    for k in obj:
        if k not in allowed:
            problems.append(f"{where}: unknown key '{k}'{_suggest(k, allowed)}")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_steps(steps, where: str, adapter: str, problems: list[str]):
    if not isinstance(steps, list):
        problems.append(f"{where}: must be a list of steps")
        return
    for j, st in enumerate(steps):
        w = f"{where}[{j}]"
        if not isinstance(st, dict) or not st:
            problems.append(f"{w}: a step must be an object like {{\"js\": \"...\"}}")
            continue
        verbs = [k for k in st if k in STEP_KEYS]
        if len(verbs) != 1:
            first = next(iter(st))
            if not verbs:
                problems.append(f"{w}: unknown step '{first}'{_suggest(first, STEP_KEYS)}; "
                                f"expected one of {', '.join(sorted(STEP_KEYS))}")
            else:
                problems.append(f"{w}: a step has exactly one verb, found {verbs}")
            continue
        verb = verbs[0]
        _check_keys(st, {verb} | STEP_KEYS[verb], w, problems)
        v = st[verb]
        if verb in ("js", "click", "waitFor", "css", "type") and not isinstance(v, str):
            problems.append(f"{w}.{verb}: must be a string")
        elif verb == "mark" and v not in ("in", "out"):
            problems.append(f"{w}.mark: must be 'in' or 'out' (trims a clip to the frames between)")
        elif verb == "key":
            if not isinstance(v, str):
                problems.append(f"{w}.key: must be a key name like 'KeyW' or 'Enter'")
            if st.get("action", "press") not in ("down", "up", "press"):
                problems.append(f"{w}.action: must be down, up or press")
        elif verb == "tap" and not (isinstance(v, list) and len(v) == 2 and all(_num(x) for x in v)):
            problems.append(f"{w}.tap: must be [x, y] in CSS pixels")
        elif verb in ("wait", "frames") and not (_num(v) and v >= 0):
            problems.append(f"{w}.{verb}: must be a non-negative number")
        elif verb == "camera":
            if not isinstance(v, dict):
                problems.append(f"{w}.camera: must be an object with {sorted(CAMERA_KEYS)}")
            else:
                _check_keys(v, CAMERA_KEYS, f"{w}.camera", problems)
            if adapter != "strata-kit":
                problems.append(f"{w}.camera: only the strata-kit adapter has a camera; use a js step")
        elif verb == "repeat":
            if not (isinstance(v, int) and v >= 0):
                problems.append(f"{w}.repeat: must be a non-negative integer")
            validate_steps(st.get("steps", []), f"{w}.steps", adapter, problems)


def validate(data: Any) -> list[str]:
    """Return a list of problems (empty when the manifest is valid)."""
    p: list[str] = []
    if not isinstance(data, dict):
        return ["manifest: top level must be a JSON object"]
    _check_keys(data, TOP_KEYS, "manifest", p)
    if "out" in data and not isinstance(data["out"], str):
        p.append("out: must be a path relative to the manifest")
    if not isinstance(data.get("vars", {}), dict):
        p.append("vars: must be an object of name -> value")
    devices = data.get("devices", {})
    if not isinstance(devices, dict):
        p.append("devices: must be an object of name -> {width, height, ...}")
        devices = {}
    for dn, d in devices.items():
        if not isinstance(d, dict):
            p.append(f"devices.{dn}: must be an object")
            continue
        _check_keys(d, DEVICE_KEYS, f"devices.{dn}", p)
        for k in ("width", "height"):
            if not (isinstance(d.get(k), int) and d[k] > 0):
                p.append(f"devices.{dn}.{k}: required positive integer")
    budgets = data.get("budgets", {})
    if not isinstance(budgets, dict):
        p.append("budgets: must be an object")
    else:
        _check_keys(budgets, set(DEFAULT_BUDGETS), "budgets", p)
    shots = data.get("shots")
    if not isinstance(shots, list) or not shots:
        p.append("shots: required, a non-empty list")
        return p
    if not isinstance(data.get("defaults", {}), dict):
        p.append("defaults: must be an object of shot fields")
        return p
    if p:
        return p
    m = Manifest(Path("manifest.json"), data, dict(data.get("vars", {})))
    known_vars = set(data.get("vars", {})) | BUILTIN_VARS
    names: dict[str, int] = {}
    all_names = {s.get("name") for s in shots if isinstance(s, dict)}
    for shot in m.shots:
        s = shot.raw
        w = f"shots[{shot.index}]" + (f" ({s['name']})" if isinstance(s.get("name"), str) else "")
        name = s.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            p.append(f"{w}.name: required; letters, digits, '.', '_' and '-' only")
        elif name in names:
            p.append(f"{w}.name: duplicate of shots[{names[name]}]")
        else:
            names[name] = shot.index
        kind = s.get("kind", "still")
        if kind not in KINDS:
            p.append(f"{w}.kind: must be one of {', '.join(KINDS)}{_suggest(str(kind), KINDS)}")
            continue
        backend = s.get("backend", "web")
        allowed = set(COMMON_SHOT_KEYS)
        if kind == "pair":
            allowed |= PAIR_KEYS
            if "of" in s:
                if s["of"] not in all_names:
                    p.append(f"{w}.of: no shot named '{s['of']}'{_suggest(str(s['of']), all_names)}")
            elif not ("before" in s and "after" in s):
                p.append(f"{w}: a pair needs 'of' (a shot shot with --tag before/after) or 'before' + 'after' image paths")
            labels = s.get("labels", ["before", "after"])
            if not (isinstance(labels, list) and len(labels) == 2):
                p.append(f"{w}.labels: must be two strings")
        else:
            if backend not in BACKENDS:
                p.append(f"{w}.backend: must be one of {', '.join(BACKENDS)}{_suggest(str(backend), BACKENDS)}")
                continue
            allowed |= WEB_KEYS if backend == "web" else COMMAND_KEYS
            dev = s.get("device", "desktop")
            if isinstance(dev, str) and dev not in DEVICES and dev not in devices:
                p.append(f"{w}.device: unknown device '{dev}'{_suggest(dev, list(DEVICES) + list(devices))}")
            elif isinstance(dev, dict):
                _check_keys(dev, DEVICE_KEYS, f"{w}.device", p)
            if backend == "web":
                if not isinstance(s.get("url"), str):
                    p.append(f"{w}.url: required for the web backend")
                adapter = s.get("adapter", "hook")
                if adapter not in ADAPTERS:
                    p.append(f"{w}.adapter: must be one of {', '.join(ADAPTERS)}{_suggest(str(adapter), ADAPTERS)}")
                for key in ("setup", "steps"):
                    if key in s:
                        validate_steps(s[key], f"{w}.{key}", adapter, p)
                css = s.get("css")
                if css is not None and not isinstance(css, str):
                    p.append(f"{w}.css: must be a string of CSS")
                for unknown in placeholders(s.get("url", "")) - known_vars:
                    p.append(f"{w}.url: unknown variable '{{{unknown}}}'{_suggest(unknown, known_vars)}")
            else:
                argv = s.get("argv")
                if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
                    p.append(f"{w}.argv: required, a non-empty list of strings")
                env = s.get("env", {})
                if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
                    p.append(f"{w}.env: must be an object of string values")
                for unknown in (placeholders(argv or []) | placeholders(env)) - known_vars:
                    p.append(f"{w}: unknown variable '{{{unknown}}}' in argv/env{_suggest(unknown, known_vars)}")
                if "repeat" in s and not (isinstance(s["repeat"], int) and s["repeat"] >= 1):
                    p.append(f"{w}.repeat: must be a positive integer")
        _check_keys(s, allowed, w, p)
        fmt = s.get("format", "jpg")
        if fmt not in ("jpg", "png"):
            p.append(f"{w}.format: must be jpg or png")
        gif = s.get("gif", {})
        if gif is not False and not isinstance(gif, dict):
            p.append(f"{w}.gif: must be an object or false")
        elif isinstance(gif, dict):
            _check_keys(gif, GIF_KEYS, f"{w}.gif", p)
            if gif.get("palette", "octree") not in PALETTES:
                p.append(f"{w}.gif.palette: must be one of {', '.join(PALETTES)}")
        video = s.get("video", [])
        if isinstance(video, str):
            video = [video]
        if not isinstance(video, list) or any(v not in VIDEO_FORMATS for v in video):
            p.append(f"{w}.video: must be a list drawn from {list(VIDEO_FORMATS)}")
        if kind != "clip":
            for k in ("gif", "video", "sheet"):
                if k in s and s[k] and kind == "still":
                    p.append(f"{w}.{k}: only clips have {k} output")
        crop = s.get("crop")
        if crop is not None and not (isinstance(crop, list) and len(crop) == 4 and all(_num(x) for x in crop)):
            p.append(f"{w}.crop: must be [x, y, width, height] in CSS pixels")
        aspect = s.get("aspect")
        if aspect is not None and not (isinstance(aspect, str) and re.fullmatch(r"\d+:\d+", aspect)):
            p.append(f"{w}.aspect: must look like '16:9' or '1:1'")
        if "speed" in s and not (_num(s["speed"]) and s["speed"] > 0):
            p.append(f"{w}.speed: must be a positive number (say so in the alt text when > 1)")
        if "retries" in s and not (isinstance(s["retries"], int) and s["retries"] >= 0):
            p.append(f"{w}.retries: must be a non-negative integer")
    return p
