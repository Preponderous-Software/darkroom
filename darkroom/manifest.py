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
import shlex
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

KINDS = ("still", "clip", "pair", "grid")
# Kinds composed from other shots' takes (no backend, never captured themselves).
COMPOSITE_KINDS = ("pair", "grid")
BACKENDS = ("web", "command")
ADAPTERS = ("strata-kit", "hook")
PALETTES = ("octree", "median", "adaptive")
VIDEO_FORMATS = ("mp4", "webm")

TOP_KEYS = {"$schema", "out", "vars", "devices", "budgets", "defaults", "shots", "description"}
COMMON_SHOT_KEYS = {
    "name", "kind", "backend", "caption", "alt", "device", "retries", "timeout", "lock",
    "format", "quality", "width", "gif", "video", "poster", "sheet", "budgets", "skip", "comment",
    "aspect", "speed", "fps", "file",
}
WEB_KEYS = {"url", "adapter", "ready", "readyTimeout", "css", "setup", "steps",
            "failOnConsoleError", "waitUntil", "launchArgs", "crop", "userAgent", "block", "each"}
COMMAND_KEYS = {"argv", "env", "cwd", "xvfb", "repeat", "glob", "each"}
PAIR_KEYS = {"of", "before", "after", "labels"}
GRID_KEYS = {"of", "tiles", "frames", "cols", "title", "captions"}
GRID_TILE_KEYS = {"shot", "frame", "image", "caption"}
SHEET_KEYS = {"labels", "cols", "width"}
SHEET_LABELS = ("none", "index", "caption")
# List fields a shot can extend with "KEY+" instead of replacing the default's list.
APPENDABLE = ("argv", "steps", "setup", "block", "launchArgs")
STILL_EXTS = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png"}
CLIP_EXTS = (".gif", ".mp4", ".webm")
GIF_KEYS = {"fps", "width", "palette", "colors", "dither", "loop", "budget_mb"}
DEVICE_KEYS = {"width", "height", "dpr", "touch", "mobile"}

STEP_KEYS = {
    "js": set(), "key": {"action"}, "tap": set(), "click": set(), "wait": set(),
    "waitFor": {"timeout"}, "frames": set(), "camera": set(), "capture": set(),
    "repeat": {"steps", "var"}, "css": set(), "type": set(), "mark": set(),
}
CAMERA_KEYS = {"yaw", "pitch", "dist", "tx", "ty", "tz"}

_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SPLICE = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")

# Placeholders the backends fill in themselves (always allowed in templates).
BUILTIN_VARS = {"out_dir", "work_dir", "width", "height", "dpr", "display", "name", "tag", "i", "n",
                "value", "manifest_dir"}


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


def expand_argv(argv: list[str], variables: dict[str, Any]) -> list[str]:
    """Template every argv entry; a whole entry ``@name`` naming a var is replaced by that var's
    items (a list var splices several entries, a scalar var gives one). ``@word`` that is not a
    var is left alone, so ``@file`` arguments survive."""
    out: list[str] = []
    for a in argv:
        m = _SPLICE.fullmatch(a) if isinstance(a, str) else None
        if m and m.group(1) in variables:
            val = variables[m.group(1)]
            items = val if isinstance(val, list) else [val]
            out.extend(str(template(x, variables)) for x in items)
        else:
            out.append(str(template(a, variables)))
    return out


def splices(argv) -> set[str]:
    return {m.group(1) for a in argv if isinstance(a, str) and (m := _SPLICE.fullmatch(a))}


def slug(value: Any) -> str:
    """A file-name-safe form of an ``each`` value ("Late summer" -> "late-summer")."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-.").lower()
    return s or "x"


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
        return None if self.kind in COMPOSITE_KINDS else self.raw.get("backend", "web")

    @property
    def composite(self) -> bool:
        return self.kind in COMPOSITE_KINDS


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
                elif k.endswith("+") and k[:-1] in APPENDABLE and isinstance(v, list) and k[:-1] not in s:
                    # "argv+": [...] extends the default's argv instead of replacing it.
                    base = merged.pop(k[:-1], None)
                    merged[k[:-1]] = (base if isinstance(base, list) else []) + copy.deepcopy(v)
                else:
                    merged[k] = v
            own = set(s) | {k[:-1] for k in s if k.endswith("+")}
            # Defaults that belong to another backend (or to no backend, for pairs) are dropped.
            if merged.get("kind") in COMPOSITE_KINDS:
                foreign = WEB_KEYS | COMMAND_KEYS | {"backend", "device"}
            elif merged.get("backend", "web") == "command":
                foreign = WEB_KEYS - COMMAND_KEYS
            else:
                foreign = COMMAND_KEYS - WEB_KEYS
            for k in list(merged):
                if k in foreign and k not in own:
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
                           "device": self.device(shot.get("device", "desktop")) if not shot.composite else None},
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
    for k, v in (overrides or {}).items():
        # --var on a list var is split like a shell would ("--fast --seed 7").
        variables[k] = shlex.split(v) if isinstance(variables.get(k), list) and isinstance(v, str) else v
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


def _validate_grid(s: dict, w: str, all_names: set, composites: dict, p: list[str]) -> None:
    has_of, has_tiles = "of" in s, "tiles" in s
    if has_of == has_tiles:
        p.append(f"{w}: a grid needs exactly one of 'of' (a shot whose take's frames become tiles) "
                 f"or 'tiles' (a list of shot names or {{\"image\": path}})")
    if has_of and s["of"] not in all_names:
        p.append(f"{w}.of: no shot named '{s['of']}'{_suggest(str(s['of']), all_names)}")
    if has_of and s["of"] == s.get("name"):
        p.append(f"{w}.of: a grid cannot be made of itself")
    elif has_of and s["of"] in composites:
        p.append(f"{w}.of: '{s['of']}' is a {composites[s['of']]}, which has no take; name a still or clip")
    tiles = s.get("tiles", [])
    if has_tiles and not (isinstance(tiles, list) and tiles):
        p.append(f"{w}.tiles: must be a non-empty list")
        tiles = []
    for j, t in enumerate(tiles if isinstance(tiles, list) else []):
        tw = f"{w}.tiles[{j}]"
        if isinstance(t, str):
            t = {"shot": t}
        if not isinstance(t, dict):
            p.append(f"{tw}: a shot name or an object like {{\"shot\": \"spring\", \"caption\": \"Spring\"}}")
            continue
        _check_keys(t, GRID_TILE_KEYS, tw, p)
        if ("shot" in t) == ("image" in t):
            p.append(f"{tw}: needs exactly one of 'shot' or 'image'")
        elif "shot" in t and t["shot"] not in all_names:
            p.append(f"{tw}.shot: no shot named '{t['shot']}'{_suggest(str(t['shot']), all_names)}")
        elif "shot" in t and t["shot"] in composites:
            p.append(f"{tw}.shot: '{t['shot']}' is a {composites[t['shot']]}, which has no take; name a still or clip")
        if "frame" in t and not isinstance(t["frame"], int):
            p.append(f"{tw}.frame: must be a frame index (negative counts from the end)")
        if "caption" in t and not isinstance(t["caption"], str):
            p.append(f"{tw}.caption: must be a string")
    frames = s.get("frames")
    if frames is not None and not (isinstance(frames, list) and frames and all(isinstance(i, int) for i in frames)):
        p.append(f"{w}.frames: must be a list of frame indexes into the take of 'of'")
    if frames is not None and has_tiles:
        p.append(f"{w}.frames: only for 'of'; give each tile its own 'frame'")
    caps = s.get("captions")
    if caps is not None and not (isinstance(caps, list) and all(isinstance(c, str) for c in caps)):
        p.append(f"{w}.captions: must be a list of strings, one per tile ('' for none)")
    if "cols" in s and not (isinstance(s["cols"], int) and s["cols"] > 0):
        p.append(f"{w}.cols: must be a positive integer")
    if "title" in s and not isinstance(s["title"], str):
        p.append(f"{w}.title: must be a string")


def validate(data: Any) -> list[str]:
    """Return a list of problems (empty when the manifest is valid)."""
    p: list[str] = []
    if not isinstance(data, dict):
        return ["manifest: top level must be a JSON object"]
    _check_keys(data, TOP_KEYS, "manifest", p)
    if "out" in data and not isinstance(data["out"], str):
        p.append("out: must be a path relative to the manifest")
    vars_ = data.get("vars", {})
    if not isinstance(vars_, dict):
        p.append("vars: must be an object of name -> value")
    else:
        for k, v in vars_.items():
            if isinstance(v, list):
                if not all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in v):
                    p.append(f"vars.{k}: a list var holds strings or numbers only (it splices into argv as '@{k}')")
            elif isinstance(v, (dict, bool)) or v is None:
                p.append(f"vars.{k}: must be a string, a number, or a list of them")
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
    list_vars = {k for k, v in data.get("vars", {}).items() if isinstance(v, list)}

    def no_list_in_strings(value, where):
        for name in sorted(placeholders(value) & list_vars):
            p.append(f"{where}: '{{{name}}}' is a list var; splice it into argv as a whole entry "
                     f"\"@{name}\" (it cannot go inside a string)")
    names: dict[str, int] = {}
    all_names = {s.get("name") for s in shots if isinstance(s, dict)}
    composites = {s.get("name"): s.get("kind") for s in shots
                  if isinstance(s, dict) and s.get("kind") in COMPOSITE_KINDS}
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
        raw = data["shots"][shot.index] if isinstance(data["shots"][shot.index], dict) else {}
        appended = set()
        for k, v in raw.items():
            if not k.endswith("+"):
                continue
            if k[:-1] not in APPENDABLE:
                p.append(f"{w}.{k}: only {', '.join(a + '+' for a in APPENDABLE)} extend a default list")
            elif k[:-1] in raw:
                p.append(f"{w}.{k}: has both '{k[:-1]}' and '{k}'; use '{k[:-1]}' to replace the "
                         f"default or '{k}' to extend it, not both")
            elif not isinstance(v, list):
                p.append(f"{w}.{k}: must be a list (it is appended to the default '{k[:-1]}')")
            else:
                appended.add(k)
        backend = s.get("backend", "web")
        allowed = set(COMMON_SHOT_KEYS) | appended
        if kind == "grid":
            allowed |= GRID_KEYS
            _validate_grid(s, w, all_names, composites, p)
        elif kind == "pair":
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
                no_list_in_strings(s.get("url", ""), f"{w}.url")
                no_list_in_strings(s.get("ready") or "", f"{w}.ready")
            else:
                argv = s.get("argv")
                if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
                    p.append(f"{w}.argv: required, a non-empty list of strings")
                env = s.get("env", {})
                if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
                    p.append(f"{w}.env: must be an object of string values")
                for unknown in (placeholders(argv or []) | placeholders(env)) - known_vars:
                    p.append(f"{w}: unknown variable '{{{unknown}}}' in argv/env{_suggest(unknown, known_vars)}")
                if isinstance(argv, list):
                    no_list_in_strings([a for a in argv if not (isinstance(a, str) and splices([a]))], f"{w}.argv")
                no_list_in_strings(env, f"{w}.env")
                cwd = s.get("cwd")
                if cwd is not None:
                    if not isinstance(cwd, str):
                        p.append(f"{w}.cwd: must be a path (default: a fresh scratch dir per run, {{work_dir}})")
                    else:
                        no_list_in_strings(cwd, f"{w}.cwd")
                if "repeat" in s and not (isinstance(s["repeat"], int) and s["repeat"] >= 1):
                    p.append(f"{w}.repeat: must be a positive integer")
            if "each" in s:
                each = s["each"]
                if not (isinstance(each, list) and each
                        and all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in each)):
                    p.append(f"{w}.each: must be a non-empty list of strings or numbers (one run per value)")
                elif "repeat" in s and s["repeat"] != len(each):
                    p.append(f"{w}.repeat: 'each' already runs {len(each)} time(s); drop 'repeat' "
                             f"(it is {s['repeat']!r})")
        _check_keys(s, allowed, w, p)
        fmt = s.get("format", "jpg")
        if fmt not in ("jpg", "png"):
            p.append(f"{w}.format: must be jpg or png")
        if "file" in s:
            f = s["file"]
            ext = Path(f).suffix.lower() if isinstance(f, str) else ""
            exts = CLIP_EXTS if kind == "clip" else tuple(STILL_EXTS)
            if not isinstance(f, str) or not f.strip():
                p.append(f"{w}.file: must be a path relative to the manifest, e.g. 'docs/images/{name}.png'")
            elif Path(f).is_absolute():
                p.append(f"{w}.file: must be relative to the manifest (absolute paths don't travel)")
            elif ext not in exts:
                p.append(f"{w}.file: a {kind} is written as {', '.join(exts)}; the extension picks the "
                         f"format (got '{ext or 'no extension'}')")
            elif kind != "clip" and "format" in s and STILL_EXTS[ext] != fmt:
                p.append(f"{w}.format: '{fmt}' disagrees with file '{f}'; drop 'format' (the extension decides)")
            else:
                for unknown in placeholders(f) - known_vars:
                    p.append(f"{w}.file: unknown variable '{{{unknown}}}'{_suggest(unknown, known_vars)}")
        sheet = s.get("sheet")
        if isinstance(sheet, dict):
            _check_keys(sheet, SHEET_KEYS, f"{w}.sheet", p)
            if sheet.get("labels", "index") not in SHEET_LABELS:
                p.append(f"{w}.sheet.labels: must be one of {', '.join(SHEET_LABELS)}")
            for k in ("cols", "width"):
                if k in sheet and not (isinstance(sheet[k], int) and sheet[k] > 0):
                    p.append(f"{w}.sheet.{k}: must be a positive integer")
        elif sheet is not None and not isinstance(sheet, bool):
            p.append(f"{w}.sheet: must be true/false or {{ \"labels\": \"none|index|caption\", \"cols\", \"width\" }}")
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
