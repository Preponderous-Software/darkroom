"""Manifest loading, validation and templating.

A manifest is a JSON file (conventionally ``media.json``). Validation is hand-written so
that every error names the exact place it happened, e.g. ``shots[2].steps[4]``.
"""
from __future__ import annotations

import copy
import difflib
import glob as _glob
import hashlib
import json
import os
import re
import shlex
import shutil
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

KINDS = ("still", "clip", "pair", "grid", "prep")
# Kinds composed from other shots' takes (no backend, never captured themselves).
COMPOSITE_KINDS = ("pair", "grid")
BACKENDS = ("web", "command")
ADAPTERS = ("strata-kit", "hook")
PALETTES = ("octree", "median", "adaptive")
VIDEO_FORMATS = ("mp4", "webm")

TOP_KEYS = {"$schema", "out", "vars", "devices", "budgets", "defaults", "shots", "description", "gallery"}
COMMON_SHOT_KEYS = {
    "name", "kind", "backend", "caption", "alt", "device", "retries", "timeout", "lock",
    "lockTimeout", "format", "quality", "width", "gif", "video", "poster", "sheet", "budgets", "skip", "comment",
    "aspect", "speed", "fps", "file", "gallery", "every", "colors", "inputs", "depends", "vars",
}
WEB_KEYS = {"url", "adapter", "ready", "readyTimeout", "css", "setup", "steps",
            "failOnConsoleError", "waitUntil", "launchArgs", "crop", "userAgent", "block", "each",
            "screenshotTimeout", "frameStep", "initScript", "context", "failOnConsole", "session"}
COMMAND_KEYS = {"argv", "env", "cwd", "xvfb", "repeat", "glob", "each", "outputs"}
# A prep runs a command that writes files (no images): only these keys apply.
PREP_KEYS = {"name", "kind", "backend", "comment", "skip", "lock", "lockTimeout", "timeout", "retries", "inputs",
             "depends", "device", "argv", "env", "cwd", "xvfb", "outputs", "vars"}
OUTPUT_KEYS = {"file", "caption", "alt"}
# Browser-context options darkroom sets itself, and the shot field that sets each.
CONTEXT_OWNED = {"viewport": "device", "deviceScaleFactor": "device (dpr)", "hasTouch": "device (touch)",
                 "isMobile": "device (mobile)", "userAgent": "userAgent", "screen": "device"}
PAIR_KEYS = {"of", "before", "after", "labels"}
GRID_KEYS = {"of", "tiles", "frames", "cols", "title", "captions"}
GRID_TILE_KEYS = {"shot", "frame", "image", "caption"}
SHEET_KEYS = {"labels", "cols", "width"}
SHEET_LABELS = ("none", "index", "caption")
# List fields a shot can extend with "KEY+" instead of replacing the default's list.
APPENDABLE = ("argv", "steps", "setup", "block", "launchArgs")
STILL_EXTS = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png"}
CLIP_EXTS = (".gif", ".mp4", ".webm")
GIF_KEYS = {"fps", "width", "palette", "colors", "dither", "loop", "budget_mb", "hold_last_ms", "every"}
VIDEO_KEYS = {"formats", "every", "hold_last"}
GALLERY_KEYS = {"width", "poster", "row"}
# The manifest's top-level `gallery`: how `darkroom gallery` lays the shots out by default.
TOP_GALLERY_KEYS = {"layout", "by", "widths"}
GALLERY_LAYOUTS = ("list", "table")
GALLERY_BY = ("device",)
# Playwright console message types (plus "pageerror", an uncaught exception), for failOnConsole.
CONSOLE_TYPES = ("error", "warning", "info", "log", "debug", "pageerror", "assert", "trace", "dir",
                 "dirxml", "table", "clear", "count", "timeEnd", "startGroup", "startGroupCollapsed",
                 "endGroup", "profile", "profileEnd")
# Fields every shot of one web `session` shares: the page is opened (and `setup` run) once, with
# the first shot's values.
SESSION_FIELDS = ("url", "adapter", "ready", "readyTimeout", "waitUntil", "css", "setup", "device",
                  "userAgent", "block", "launchArgs", "initScript", "context", "frameStep", "lock")
CAPTURE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
DEVICE_KEYS = {"width", "height", "dpr", "touch", "mobile"}

STEP_KEYS = {
    "js": set(), "key": {"action"}, "tap": {"force"}, "click": {"force"}, "wait": set(),
    "waitFor": {"timeout"}, "frames": {"step"}, "camera": set(), "capture": set(),
    "repeat": {"steps", "var"}, "css": set(), "type": set(), "mark": set(),
    "until": {"steps", "max", "then", "var"}, "goto": set(), "reload": set(),
    "stable": {"timeout"},
}

# What changes the captured pixels (a reshoot) and what changes only the encoding (re-encode
# the saved take). Anything else (caption, alt, gallery, retries, lock, timeouts, comment,
# skip, failOnConsoleError) changes neither, so editing it never marks a shot stale.
CAPTURE_FIELDS = {"kind", "backend", "device", "url", "adapter", "ready", "waitUntil", "css", "setup",
                  "steps", "crop", "userAgent", "block", "launchArgs", "each", "frameStep",
                  "argv", "env", "cwd", "xvfb", "repeat", "glob", "initScript", "context"}
ENCODE_FIELDS = {"format", "quality", "width", "gif", "video", "poster", "sheet", "aspect", "speed",
                 "fps", "file", "every", "colors", "of", "before", "after", "labels", "tiles",
                 "frames", "cols", "title", "captions"}
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
        if self.kind == "prep":
            return "command"
        return None if self.kind in COMPOSITE_KINDS else self.raw.get("backend", "web")

    @property
    def composite(self) -> bool:
        return self.kind in COMPOSITE_KINDS

    @property
    def prep(self) -> bool:
        return self.kind == "prep"

    def named_outputs(self) -> list[tuple[str, dict]]:
        """A command still's ``outputs`` as (key, {"file", "caption"?, "alt"?}) in order."""
        outs = self.raw.get("outputs")
        if self.kind != "still" or not isinstance(outs, dict):
            return []
        return [(k, {"file": v} if isinstance(v, str) else dict(v)) for k, v in outs.items()]


@dataclass
class Manifest:
    path: Path
    data: dict[str, Any]
    vars: dict[str, Any] = field(default_factory=dict)
    # `shoot --out DIR`: every output (including `file` ones) goes here instead.
    out_override: Path | None = None
    # `--var NAME=VALUE`: these win over a shot's own `vars` too.
    overrides: dict[str, Any] = field(default_factory=dict)

    def shot_vars(self, shot: Shot) -> dict[str, Any]:
        """The vars a shot sees: its own ``vars`` merged over the manifest's (``--var`` still
        wins), expanded like the manifest's. Without shot vars, exactly ``self.vars``."""
        own = shot.get("vars")
        if not isinstance(own, dict) or not own:
            return self.vars
        variables = dict(self.vars)
        variables.update(own)
        variables.update({k: v for k, v in self.overrides.items() if k in variables})
        return expand_vars(variables)

    def device_name(self, shot: Shot, variables: dict | None = None) -> str | None:
        """The shot's device name with ``{var}`` filled in (None for an inline device)."""
        dev = shot.get("device", "desktop")
        if isinstance(dev, dict):
            return None
        return str(template(dev, self.shot_vars(shot) if variables is None else variables))

    def shot_device(self, shot: Shot, variables: dict | None = None) -> dict[str, Any]:
        """The device spec for a shot (or one ``each`` run, given its variables)."""
        dev = shot.get("device", "desktop")
        if isinstance(dev, str) and placeholders(dev):
            name = self.device_name(shot, variables)
            if placeholders(name) or (name not in DEVICES and name not in self.data.get("devices", {})):
                raise KeyError(f"{shot.name}: device '{dev}' gives '{name}', which is not a device"
                               + _suggest(name, list(DEVICES) + list(self.data.get("devices", {}))))
            return self.device(name)
        return self.device(dev)

    def device_varies(self, shot: Shot) -> bool:
        """Does the device change from one ``each`` run to the next (``"device": "{value}"``)?"""
        dev = shot.get("device")
        return isinstance(dev, str) and bool(placeholders(dev) & {"value", "i"}) and bool(shot.get("each"))

    def run_vars(self, shot: Shot, i: int) -> dict[str, Any]:
        each = shot.get("each") or []
        return dict(self.shot_vars(shot), value=each[i], i=i, n=len(each))

    @property
    def dir(self) -> Path:
        return self.path.parent

    @property
    def out_dir(self) -> Path:
        if self.out_override is not None:
            return Path(self.out_override).resolve()
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
            if merged.get("kind") == "prep":
                # A prep takes only what applies to it from the defaults (no formats, captions...).
                foreign = {k for k in merged if k not in PREP_KEYS} | {"backend"}
            elif merged.get("kind") in COMPOSITE_KINDS:
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

    def capture_hash(self, shot: Shot, tag: str | None) -> str:
        """Hash of what changes the captured frames: the page or program, its steps, the device
        and the vars. A change means a reshoot. Not the darkroom version, retries, locks,
        timeouts or presentation fields, so none of those make a shot stale."""
        fields = {k: shot.raw[k] for k in sorted(CAPTURE_FIELDS) if k in shot.raw}
        fields["backend"] = shot.backend
        fields["kind"] = shot.kind
        if "outputs" in shot.raw:  # which files become which outputs; not their captions
            outs = shot.raw["outputs"]
            fields["outputs"] = ({k: v["file"] if isinstance(v, dict) else v for k, v in outs.items()}
                                 if isinstance(outs, dict) else outs)
        if shot.composite:
            device = None
        elif self.device_varies(shot):  # one device per each value
            device = [self.shot_device(shot, self.run_vars(shot, i)) for i in range(len(shot["each"]))]
        else:
            device = self.shot_device(shot)
        blob = {"shot": fields, "vars": self.shot_vars(shot), "tag": tag, "device": device}
        # Only shots that read files (inputs, a prep's outputs, an init script file) get this
        # key, so the hashes of every other shot are the same as in 0.3.
        files = self.input_files(shot)
        if files:
            blob["inputs"] = files
        return _digest(blob)

    def input_patterns(self, shot: Shot) -> list[str]:
        """The paths and globs (relative to the manifest, vars filled in) whose contents feed
        a shot's capture hash: its ``inputs``, the ``outputs`` of the preps it ``depends`` on,
        and its ``initScript`` files."""
        variables = self.shot_vars(shot)
        pats = [str(x) for x in template(shot.get("inputs") or [], variables)]
        if shot.get("depends"):
            by_name = {s.name: s for s in self.shots}
            for dep in shot["depends"]:
                d = by_name.get(dep)
                if d is not None and d.prep and isinstance(d.get("outputs"), list):
                    pats += [str(x) for x in template(d["outputs"], self.shot_vars(d))]
        pats += [f for f in init_files(shot.get("initScript"), variables)]
        return pats

    def input_files(self, shot: Shot) -> dict[str, str | None]:
        """{path relative to the manifest: sha256 of its content (None if missing)} for every
        file the shot's input patterns name. A directory stands for every file under it."""
        out: dict[str, str | None] = {}
        for pat in self.input_patterns(shot):
            for rel, path in expand_input(self.dir, pat):
                out[rel] = _file_sha(path) if path is not None else None
        return out

    def encode_hash(self, shot: Shot, tag: str | None) -> str:
        """Hash of what changes the outputs made from a take (formats, budgets, palettes, sizes,
        output paths). A change alone means re-encoding the saved take, not a reshoot."""
        fields = {k: shot.raw[k] for k in sorted(ENCODE_FIELDS) if k in shot.raw}
        gallery = shot.raw.get("gallery")
        if isinstance(gallery, dict) and gallery.get("poster"):
            fields["gallery.poster"] = True  # makes a poster file; gallery.width does not
        return _digest({"shot": fields, "budgets": self.budgets(shot), "tag": tag,
                        "out": str(self.out_dir), "file_vars": template(shot.raw.get("file"), self.shot_vars(shot))})

    def legacy_hash(self, shot: Shot, tag: str | None, version: str) -> str:
        """The 0.1/0.2 whole-shot hash, to recognise state written by those versions."""
        blob = json.dumps({"shot": shot.raw, "vars": self.vars, "tag": tag,
                           "budgets": self.budgets(shot), "version": version,
                           "device": self.device(shot.get("device", "desktop")) if not shot.composite else None},
                          sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def shot_hash(self, shot: Shot, tag: str | None) -> str:
        """Both hashes in one string (capture, then encode)."""
        return self.capture_hash(shot, tag) + "." + self.encode_hash(shot, tag)


def init_files(init, variables: dict) -> list[str]:
    """The ``{"file": path}`` entries of an ``initScript`` value, vars filled in."""
    items = init if isinstance(init, list) else [init] if init is not None else []
    return [str(template(i["file"], variables)) for i in items if isinstance(i, dict) and isinstance(i.get("file"), str)]


def expand_input(base: Path, pattern: str) -> list[tuple[str, Path | None]]:
    """(relative name, path) for each file a pattern names: a glob's matches, every file under a
    directory, or the file itself. A pattern that names nothing gives one (pattern, None)."""
    full = os.path.join(str(base), pattern)
    if _glob.has_magic(pattern):
        hits = sorted(Path(h) for h in _glob.glob(full, recursive=True))
    else:
        hits = [Path(full)] if os.path.exists(full) else []
    files: list[Path] = []
    for h in hits:
        if h.is_dir():
            files += sorted(p for p in h.rglob("*") if p.is_file())
        elif h.is_file():
            files.append(h)
    if not files:
        return [(pattern, None)]
    return [(os.path.relpath(f, base), f) for f in files]


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


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
    return Manifest(path, data, expand_vars(variables),
                    overrides={k: variables[k] for k in overrides or {}})


def expand_vars(variables: dict[str, Any]) -> dict[str, Any]:
    """Vars may refer to other vars ("page": "file://{manifest_dir}/index.html")."""
    for _ in range(5):
        expanded = {k: template(v, variables) for k, v in variables.items()}
        if expanded == variables:
            break
        variables = expanded
    return variables


def _check_keys(obj: dict, allowed: set, where: str, problems: list[str]):
    for k in obj:
        if k not in allowed:
            problems.append(f"{where}: unknown key '{k}'{_suggest(k, allowed)}")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_steps(steps, where: str, adapter: str, problems: list[str], kind: str = "clip"):
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
        if verb in ("tap", "click") and isinstance(v, dict):
            _check_keys(v, {"at", "of"}, f"{w}.{verb}", problems)
            at, of = v.get("at"), v.get("of")
            if not (isinstance(at, list) and len(at) == 2 and all(_num(x) and 0 <= x <= 1 for x in at)):
                problems.append(f"{w}.{verb}.at: must be [fx, fy], fractions (0 to 1) of the element's box "
                                f"([0.5, 0.5] is its centre)")
            if not (isinstance(of, str) and of.strip()):
                problems.append(f"{w}.{verb}.of: required, a CSS selector for the element whose box 'at' is in")
        elif verb in ("js", "click", "waitFor", "css", "type", "stable") and not isinstance(v, str):
            problems.append(f"{w}.{verb}: must be a string" + (
                " (a CSS selector), or {\"at\": [fx, fy], \"of\": \"css\"}" if verb == "click" else
                " (a CSS selector; waits until its box stops moving)" if verb == "stable" else ""))
        elif verb == "mark" and v not in ("in", "out"):
            problems.append(f"{w}.mark: must be 'in' or 'out' (trims a clip to the frames between)")
        elif verb == "key":
            if not isinstance(v, str):
                problems.append(f"{w}.key: must be a key name like 'KeyW' or 'Enter'")
            if st.get("action", "press") not in ("down", "up", "press"):
                problems.append(f"{w}.action: must be down, up or press")
        elif verb == "tap" and not (isinstance(v, str) and v.strip()
                                    or isinstance(v, list) and len(v) == 2 and all(_num(x) for x in v)):
            if not isinstance(v, dict):
                problems.append(f"{w}.tap: must be [x, y] in CSS pixels or a CSS selector (tapped at its centre), "
                                f"or {{\"at\": [fx, fy], \"of\": \"css\"}} (fractions of the element's box)")
        elif verb in ("wait", "frames") and not (_num(v) and v >= 0):
            problems.append(f"{w}.{verb}: must be a non-negative number")
        elif verb == "goto" and not (isinstance(v, str) and v.strip()):
            problems.append(f"{w}.goto: must be a URL (absolute, or relative to the current page)")
        elif verb == "reload" and v is not True:
            problems.append(f"{w}.reload: must be true")
        elif verb == "capture":
            if isinstance(v, str):
                if kind != "still":
                    problems.append(f"{w}.capture: a named capture ('{v}') makes a still file; in a "
                                    f"{kind} use {{\"capture\": true}}")
                elif not CAPTURE_NAME.fullmatch(_VAR.sub("x", v)):
                    problems.append(f"{w}.capture: '{v}' is not a file name; letters, digits, '.', '_' and '-' only "
                                    f"(and {{var}} placeholders)")
            elif v is not True:
                problems.append(f"{w}.capture: must be true (a clip frame) or a name (a still written as NAME.jpg)")
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
            validate_steps(st.get("steps", []), f"{w}.steps", adapter, problems, kind)
        elif verb == "until":
            if not (isinstance(v, str) and v.strip()):
                problems.append(f"{w}.until: must be a JS expression; the steps repeat until it is truthy")
            mx = st.get("max")
            if not (isinstance(mx, int) and not isinstance(mx, bool) and mx >= 1):
                problems.append(f"{w}.max: required, a positive integer (the most passes before giving up)")
            then = st.get("then", 0)
            if not (isinstance(then, int) and not isinstance(then, bool) and then >= 0):
                problems.append(f"{w}.then: must be a non-negative integer (passes to run after the condition holds)")
            if "steps" not in st:
                problems.append(f"{w}.steps: required, the steps to repeat")
            validate_steps(st.get("steps", []), f"{w}.steps", adapter, problems, kind)
        if "force" in st and not isinstance(st["force"], bool):
            problems.append(f"{w}.force: must be true or false (true skips Playwright's actionability checks)")
        if verb == "stable" and "timeout" in st and not (_num(st["timeout"]) and st["timeout"] > 0):
            problems.append(f"{w}.timeout: must be a positive number of milliseconds")
        if verb == "frames" and "step" in st:
            if adapter != "strata-kit":
                problems.append(f"{w}.step: only the strata-kit adapter drives a clock; drop 'step'")
            elif not (_num(st["step"]) and st["step"] > 0):
                problems.append(f"{w}.step: must be a positive number of seconds per frame (e.g. 0.1)")


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


def _every_ok(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def still_ext(s: dict) -> str:
    f = s.get("file")
    if isinstance(f, str) and Path(f).suffix.lower() in STILL_EXTS:
        return STILL_EXTS[Path(f).suffix.lower()]
    return s.get("format", "jpg")


def _named_captures(s: dict) -> list[str]:
    """Names of the ``{"capture": "name"}`` steps of a shot, in order."""
    names: list[str] = []

    def walk(steps):
        for st in steps if isinstance(steps, list) else []:
            if isinstance(st, dict):
                if isinstance(st.get("capture"), str):
                    names.append(st["capture"])
                walk(st.get("steps"))
    walk(s.get("setup"))
    walk(s.get("steps"))
    return names


def validate(data: Any) -> list[str]:
    """Return a list of problems (empty when the manifest is valid)."""
    p: list[str] = []
    if not isinstance(data, dict):
        return ["manifest: top level must be a JSON object"]
    _check_keys(data, TOP_KEYS, "manifest", p)
    if "out" in data and not isinstance(data["out"], str):
        p.append("out: must be a path relative to the manifest")
    _validate_vars(data.get("vars", {}), "vars", p)
    _validate_top_gallery(data.get("gallery"), p)
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
    top_known = set(data.get("vars", {})) | BUILTIN_VARS
    list_vars: set[str] = set()

    def no_list_in_strings(value, where):
        for name in sorted(placeholders(value) & list_vars):
            p.append(f"{where}: '{{{name}}}' is a list var; splice it into argv as a whole entry "
                     f"\"@{name}\" (it cannot go inside a string)")
    names: dict[str, int] = {}
    all_names = {s.get("name") for s in shots if isinstance(s, dict)}
    composites = {s.get("name"): s.get("kind") for s in shots
                  if isinstance(s, dict) and s.get("kind") in COMPOSITE_KINDS + ("prep",)}
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
        # A shot's own vars are merged over the manifest's.
        own_vars = s.get("vars") if isinstance(s.get("vars"), dict) else {}
        if "vars" in s:
            _validate_vars(s["vars"], f"{w}.vars", p)
        known_vars = top_known | set(own_vars)
        merged_vars = {**data.get("vars", {}), **own_vars}
        list_vars = {k for k, v in merged_vars.items() if isinstance(v, list)}
        if kind == "grid":
            allowed |= GRID_KEYS
            _validate_grid(s, w, all_names, composites, p)
        elif kind == "pair":
            allowed |= PAIR_KEYS
            if "of" in s:
                if s["of"] not in all_names:
                    p.append(f"{w}.of: no shot named '{s['of']}'{_suggest(str(s['of']), all_names)}")
                elif composites.get(s["of"]) == "prep":
                    p.append(f"{w}.of: '{s['of']}' is a prep, which makes no images; name a still or clip")
            elif not ("before" in s and "after" in s):
                p.append(f"{w}: a pair needs 'of' (a shot shot with --tag before/after) or 'before' + 'after' image paths")
            labels = s.get("labels", ["before", "after"])
            if not (isinstance(labels, list) and len(labels) == 2):
                p.append(f"{w}.labels: must be two strings")
        elif kind == "prep":
            _validate_prep(s, w, known_vars, p)
            _validate_inputs(s, w, all_names, composites, known_vars, p)
            continue
        else:
            if backend not in BACKENDS:
                p.append(f"{w}.backend: must be one of {', '.join(BACKENDS)}{_suggest(str(backend), BACKENDS)}")
                continue
            allowed |= WEB_KEYS if backend == "web" else COMMAND_KEYS
            dev = s.get("device", "desktop")
            if isinstance(dev, str) and placeholders(dev):
                _validate_device_template(s, w, dev, merged_vars, known_vars, devices, p)
            elif isinstance(dev, str) and dev not in DEVICES and dev not in devices:
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
                        validate_steps(s[key], f"{w}.{key}", adapter, p, kind)
                if "screenshotTimeout" in s and not (_num(s["screenshotTimeout"]) and s["screenshotTimeout"] > 0):
                    p.append(f"{w}.screenshotTimeout: must be a positive number of milliseconds "
                             f"(default: the shot's timeout, else 120000)")
                if "frameStep" in s:
                    if adapter != "strata-kit":
                        p.append(f"{w}.frameStep: only the strata-kit adapter drives a clock")
                    elif not (_num(s["frameStep"]) and s["frameStep"] > 0):
                        p.append(f"{w}.frameStep: must be a positive number of seconds per frame (default 1/30)")
                _validate_capture_names(s, w, kind, known_vars, p)
                _validate_console(s, w, p)
                if "session" in s:
                    _validate_session_key(s, w, p)
                css = s.get("css")
                if css is not None and not isinstance(css, str):
                    p.append(f"{w}.css: must be a string of CSS")
                for unknown in placeholders(s.get("url", "")) - known_vars:
                    p.append(f"{w}.url: unknown variable '{{{unknown}}}'{_suggest(unknown, known_vars)}")
                no_list_in_strings(s.get("url", ""), f"{w}.url")
                no_list_in_strings(s.get("ready") or "", f"{w}.ready")
                _validate_init(s, w, known_vars, p)
                if "outputs" in s:
                    allowed.add("outputs")
                    p.append(f"{w}.outputs: only command shots map written files to outputs; a web "
                             f"still names its images with {{\"capture\": \"NAME\"}} steps")
            else:
                if "session" in s:
                    allowed.add("session")  # reported (pointedly) by _session_conflicts
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
                if "outputs" in s:
                    _validate_named_outputs(s, w, kind, p)
            if "each" in s:
                each = s["each"]
                if not (isinstance(each, list) and each
                        and all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in each)):
                    p.append(f"{w}.each: must be a non-empty list of strings or numbers (one run per value)")
                elif "repeat" in s and s["repeat"] != len(each):
                    p.append(f"{w}.repeat: 'each' already runs {len(each)} time(s); drop 'repeat' "
                             f"(it is {s['repeat']!r})")
        _validate_inputs(s, w, all_names, composites, known_vars, p)
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
                file_vars = known_vars | ({"key"} if isinstance(s.get("outputs"), dict) else set())
                for unknown in placeholders(f) - file_vars:
                    p.append(f"{w}.file: unknown variable '{{{unknown}}}'{_suggest(unknown, file_vars)}")
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
            if gif.get("palette", "adaptive") not in PALETTES:
                p.append(f"{w}.gif.palette: must be one of {', '.join(PALETTES)}")
            if "hold_last_ms" in gif and not (_num(gif["hold_last_ms"]) and gif["hold_last_ms"] >= 0):
                p.append(f"{w}.gif.hold_last_ms: must be milliseconds to show the last frame (e.g. 1500)")
            if "every" in gif and not _every_ok(gif["every"]):
                p.append(f"{w}.gif.every: must be a positive integer (keep every Nth captured frame)")
        video = s.get("video", [])
        if isinstance(video, dict):
            _check_keys(video, VIDEO_KEYS, f"{w}.video", p)
            if "hold_last" in video and not (_num(video["hold_last"]) and video["hold_last"] >= 0):
                p.append(f"{w}.video.hold_last: must be seconds to hold the last frame (e.g. 1.5)")
            if "every" in video and not _every_ok(video["every"]):
                p.append(f"{w}.video.every: must be a positive integer (keep every Nth captured frame)")
            video = video.get("formats", [])
        if isinstance(video, str):
            video = [video]
        if not isinstance(video, list) or any(v not in VIDEO_FORMATS for v in video):
            p.append(f"{w}.video: must be a list drawn from {list(VIDEO_FORMATS)} "
                     f"(or {{\"formats\": [...], \"every\", \"hold_last\"}})")
        if "every" in s:
            if not _every_ok(s["every"]):
                p.append(f"{w}.every: must be a positive integer (keep every Nth captured frame)")
            elif kind != "clip":
                p.append(f"{w}.every: only clips have frames to subsample")
        if "colors" in s:
            if not (isinstance(s["colors"], int) and not isinstance(s["colors"], bool) and 2 <= s["colors"] <= 256):
                p.append(f"{w}.colors: must be an integer from 2 to 256 (PNG colours)")
            elif kind == "clip":
                p.append(f"{w}.colors: is for PNG stills; a clip's GIF takes gif.colors")
            elif still_ext(s) != "png":
                p.append(f"{w}.colors: only PNG stills are quantized; set \"format\": \"png\" (or a .png file)")
        gal = s.get("gallery")
        if gal is not None:
            if not isinstance(gal, dict):
                p.append(f"{w}.gallery: must be an object like {{\"width\": 480, \"poster\": true}}")
            else:
                _check_keys(gal, GALLERY_KEYS, f"{w}.gallery", p)
                gw = gal.get("width")
                if "width" in gal and not (isinstance(gw, int) and not isinstance(gw, bool) and gw > 0):
                    p.append(f"{w}.gallery.width: must be a positive integer (display width in pixels)")
                if "poster" in gal and not isinstance(gal["poster"], bool):
                    p.append(f"{w}.gallery.poster: must be true or false")
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
        _check_lock_timeout(s, w, p)
    p += _dependency_cycles(m.shots)
    p += _session_conflicts(m)
    return p


def _validate_vars(vars_, where: str, p: list[str]) -> None:
    if not isinstance(vars_, dict):
        p.append(f"{where}: must be an object of name -> value")
        return
    for k, v in vars_.items():
        if isinstance(v, list):
            if not all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in v):
                p.append(f"{where}.{k}: a list var holds strings or numbers only (it splices into argv as '@{k}')")
        elif isinstance(v, (dict, bool)) or v is None:
            p.append(f"{where}.{k}: must be a string, a number, or a list of them")
        if k in BUILTIN_VARS and where != "vars":
            p.append(f"{where}.{k}: '{k}' is set by darkroom itself; pick another name")


def _validate_top_gallery(g, p: list[str]) -> None:
    if g is None:
        return
    if not isinstance(g, dict):
        p.append('gallery: must be an object like {"layout": "table", "by": "device"}')
        return
    _check_keys(g, TOP_GALLERY_KEYS, "gallery", p)
    if g.get("layout", "list") not in GALLERY_LAYOUTS:
        p.append(f"gallery.layout: must be one of {', '.join(GALLERY_LAYOUTS)}"
                 f"{_suggest(str(g.get('layout')), GALLERY_LAYOUTS)}")
    if "by" in g and g["by"] not in GALLERY_BY:
        p.append(f"gallery.by: must be 'device' (one column per device)")
    if "by" in g and g.get("layout", "list") != "table":
        p.append("gallery.by: only for \"layout\": \"table\"")
    widths = g.get("widths")
    if widths is not None and not (isinstance(widths, dict) and all(
            isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in widths.values())):
        p.append('gallery.widths: must be an object of device name -> image width in pixels, '
                 'e.g. {"phone": 200, "desktop": 480}')


def _validate_device_template(s: dict, w: str, dev: str, merged_vars: dict, known_vars: set,
                              devices: dict, p: list[str]) -> None:
    """``"device": "{value}"`` (or ``"{dev}"``): every value it can take must name a device."""
    unknown = placeholders(dev) - known_vars
    if unknown:
        for u in sorted(unknown):
            p.append(f"{w}.device: unknown variable '{{{u}}}'{_suggest(u, known_vars)}")
        return
    run_dependent = placeholders(dev) & {"value", "i", "n"}
    each = s.get("each") if isinstance(s.get("each"), list) else None
    if run_dependent and not each:
        p.append(f"{w}.device: '{dev}' uses {{{sorted(run_dependent)[0]}}}, which only 'each' sets")
        return
    if placeholders(dev) - {"value", "i", "n"} - set(merged_vars):
        return  # a built-in filled in at run time; nothing to check here
    variables = expand_vars({k: v for k, v in merged_vars.items()})
    known = list(DEVICES) + list(devices)
    for i, value in enumerate(each or [None]):
        run = dict(variables, **({"value": value, "i": i, "n": len(each)} if each else {}))
        name = str(template(dev, run))
        if name not in known:
            why = f" for each value '{value}'" if each else ""
            p.append(f"{w}.device: '{dev}' gives '{name}'{why}, which is not a device{_suggest(name, known)} "
                     f"(known: {', '.join(known)})")


def _validate_capture_names(s: dict, w: str, kind: str, known_vars: set, p: list[str]) -> None:
    names = _named_captures(s)
    for n in names:
        for u in sorted(placeholders(n) - known_vars):
            p.append(f"{w}: capture name '{n}': unknown variable '{{{u}}}'{_suggest(u, known_vars)}")
    if kind == "still" and "each" in s and names:
        fixed = [n for n in names if not placeholders(n) & {"value", "i"}]
        if fixed:
            p.append(f"{w}.each: capture name '{fixed[0]}' would be written once per value; with 'each', "
                     f"put {{value}} (or {{i}}) in every capture name, e.g. '{fixed[0]}-{{value}}'")
    if "session" in s and s.get("each"):
        p.append(f"{w}.session: 'each' opens a fresh page per value, so it cannot share a session; "
                 f"drop one of them")


def _validate_console(s: dict, w: str, p: list[str]) -> None:
    if "failOnConsole" in s:
        v = s["failOnConsole"]
        if not (isinstance(v, list) and all(isinstance(t, str) for t in v)):
            p.append(f"{w}.failOnConsole: must be a list of console message types, e.g. [\"error\", \"warning\"]")
        else:
            for t in v:
                if t not in CONSOLE_TYPES:
                    p.append(f"{w}.failOnConsole: unknown console type '{t}'{_suggest(t, CONSOLE_TYPES)}; "
                             f"use error, warning, info, log, debug, ...")
    if "failOnConsoleError" in s and not isinstance(s["failOnConsoleError"], bool):
        p.append(f"{w}.failOnConsoleError: must be true or false (the same as \"failOnConsole\": [\"error\"])")


def _validate_session_key(s: dict, w: str, p: list[str]) -> None:
    v = s["session"]
    if not (isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", v)):
        p.append(f"{w}.session: must be a name (letters, digits, '.', '_' and '-'); consecutive web shots "
                 f"with the same session share one page")


def session_signature(m: Manifest, shot: Shot) -> dict:
    """What every shot of one session must agree on (the page is opened once, with these)."""
    variables = m.shot_vars(shot)
    sig = {k: template(shot.raw.get(k), variables) for k in SESSION_FIELDS if k != "device"}
    try:
        sig["device"] = m.shot_device(shot)
    except KeyError:
        sig["device"] = shot.raw.get("device")
    return sig


def _session_conflicts(m: Manifest) -> list[str]:
    first: dict[str, tuple[Shot, dict]] = {}
    problems = []
    for shot in m.shots:
        name = shot.raw.get("session")
        if not isinstance(name, str) or shot.backend != "web" or shot.composite:
            if isinstance(name, str) and shot.backend == "command":
                problems.append(f"shots[{shot.index}] ({shot.raw.get('name')}).session: only web shots share a "
                                f"page; drop 'session'")
            continue
        try:
            sig = session_signature(m, shot)
        except Exception:  # noqa: BLE001 - reported elsewhere (a bad device or var)
            continue
        if name not in first:
            first[name] = (shot, sig)
            continue
        lead, lead_sig = first[name]
        for k in SESSION_FIELDS:
            if sig.get(k) != lead_sig.get(k):
                problems.append(f"shots[{shot.index}] ({shot.raw.get('name')}).{k}: differs from '{lead.raw.get('name')}', "
                                f"the first shot of session '{name}'; shots in one session share one page, so "
                                f"{k} must be the same (or use another session name)")
                break
    return problems


def _check_lock_timeout(s: dict, w: str, p: list[str]) -> None:
    v = s.get("lockTimeout")
    if v is not None and not (_num(v) and v > 0):
        p.append(f"{w}.lockTimeout: must be a positive number of seconds to wait for the lock")


def _validate_prep(s: dict, w: str, known_vars: set, p: list[str]) -> None:
    for k in s:
        if k not in PREP_KEYS:
            if k in COMMON_SHOT_KEYS | WEB_KEYS | COMMAND_KEYS | GRID_KEYS | PAIR_KEYS:
                why = ("it runs once and makes no images" if k in ("each", "repeat", "glob")
                       else "a prep makes no images" if k not in WEB_KEYS else "a prep runs a command")
                p.append(f"{w}.{k}: not for a prep ({why}); a prep takes argv, env, cwd, xvfb, "
                         f"outputs, inputs and depends")
            else:
                p.append(f"{w}: unknown key '{k}'{_suggest(k, PREP_KEYS)}")
    _check_lock_timeout(s, w, p)
    if s.get("backend", "command") != "command":
        p.append(f"{w}.backend: a prep runs a command; drop 'backend' (or set it to 'command')")
    argv = s.get("argv")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        p.append(f"{w}.argv: required, a non-empty list of strings")
    env = s.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        p.append(f"{w}.env: must be an object of string values")
    for unknown in (placeholders(argv or []) | placeholders(env)) - known_vars:
        p.append(f"{w}: unknown variable '{{{unknown}}}' in argv/env{_suggest(unknown, known_vars)}")
    if "cwd" in s and not isinstance(s["cwd"], str):
        p.append(f"{w}.cwd: must be a path (default for a prep: the manifest's folder)")
    outs = s.get("outputs")
    if outs is None:
        p.append(f"{w}.outputs: required, the files the prep writes (paths relative to the manifest), "
                 f"e.g. [\"build/stats.json\"]; they decide whether it is up to date")
    elif not (isinstance(outs, list) and outs and all(isinstance(o, str) and o.strip() for o in outs)):
        p.append(f"{w}.outputs: must be a non-empty list of file paths relative to the manifest")
    else:
        for j, o in enumerate(outs):
            if Path(o).is_absolute():
                p.append(f"{w}.outputs[{j}]: must be relative to the manifest (absolute paths don't travel)")
            elif _glob.has_magic(o):
                p.append(f"{w}.outputs[{j}]: '{o}' is a glob; name each file the prep writes")
            for unknown in placeholders(o) - known_vars:
                p.append(f"{w}.outputs[{j}]: unknown variable '{{{unknown}}}'{_suggest(unknown, known_vars)}")


def _validate_named_outputs(s: dict, w: str, kind: str, p: list[str]) -> None:
    outs = s["outputs"]
    if kind != "still":
        p.append(f"{w}.outputs: only a command still has named outputs (a {kind} keeps every image "
                 f"it writes as a frame)")
        return
    if not (isinstance(outs, dict) and outs):
        p.append(f"{w}.outputs: must be an object of name -> file the command writes into {{out_dir}}, "
                 f"e.g. {{\"top\": \"top.png\"}}")
        return
    for k, v in outs.items():
        ow = f"{w}.outputs.{k}"
        if not CAPTURE_NAME.fullmatch(k):
            p.append(f"{ow}: '{k}' is not a file name; letters, digits, '.', '_' and '-' only")
        if isinstance(v, dict):
            _check_keys(v, OUTPUT_KEYS, ow, p)
            for t in ("caption", "alt"):
                if t in v and not isinstance(v[t], str):
                    p.append(f"{ow}.{t}: must be a string")
            v = v.get("file")
            if v is None:
                p.append(f"{ow}.file: required, the file the command writes into {{out_dir}}")
                continue
        if not (isinstance(v, str) and v.strip()):
            p.append(f"{ow}: must be a file the command writes into {{out_dir}} (e.g. '{k}.png') or "
                     f"{{\"file\": ..., \"caption\": ..., \"alt\": ...}}")
        elif Path(v).is_absolute() or v.startswith(".."):
            p.append(f"{ow}: '{v}' must be relative to {{out_dir}}, where the command writes it")
        elif Path(v).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            p.append(f"{ow}: '{v}' is not an image (.png, .jpg, .jpeg, .webp or .bmp)")
    for k in ("each", "glob"):
        if k in s:
            p.append(f"{w}.{k}: '{k}' and named outputs don't mix; outputs already name every image of the one run")
    if s.get("repeat", 1) != 1:
        p.append(f"{w}.repeat: named outputs come from one run; drop 'repeat'")
    f = s.get("file")
    if isinstance(f, str) and "{key}" not in f and placeholders(f) & {"value", "i"}:
        p.append(f"{w}.file: use '{{key}}' (the output's name) in the file template, not '{{value}}'/'{{i}}'")


def _validate_inputs(s: dict, w: str, all_names: set, kinds: dict, known_vars: set, p: list[str]) -> None:
    if "inputs" in s:
        ins = s["inputs"]
        if not (isinstance(ins, list) and all(isinstance(x, str) and x.strip() for x in ins)):
            p.append(f"{w}.inputs: must be a list of paths or globs relative to the manifest, "
                     f"e.g. [\"data/*.json\"]")
        else:
            for j, x in enumerate(ins):
                if Path(x).is_absolute():
                    p.append(f"{w}.inputs[{j}]: must be relative to the manifest (absolute paths don't travel)")
                for unknown in placeholders(x) - known_vars:
                    p.append(f"{w}.inputs[{j}]: unknown variable '{{{unknown}}}'{_suggest(unknown, known_vars)}")
    if "depends" in s:
        deps = s["depends"]
        if not (isinstance(deps, list) and all(isinstance(d, str) for d in deps)):
            p.append(f"{w}.depends: must be a list of shot names that run first")
            return
        for j, d in enumerate(deps):
            if d == s.get("name"):
                p.append(f"{w}.depends[{j}]: a shot cannot depend on itself")
            elif d not in all_names:
                p.append(f"{w}.depends[{j}]: no shot named '{d}'{_suggest(d, all_names)}")


def _validate_init(s: dict, w: str, known_vars: set, p: list[str]) -> None:
    if "initScript" in s:
        init = s["initScript"]
        items = init if isinstance(init, list) else [init]
        for j, it in enumerate(items):
            iw = f"{w}.initScript" + (f"[{j}]" if isinstance(init, list) else "")
            if isinstance(it, str):
                if not it.strip():
                    p.append(f"{iw}: empty script")
                continue
            if not (isinstance(it, dict) and set(it) == {"file"} and isinstance(it["file"], str) and it["file"].strip()):
                p.append(f"{iw}: must be JS source (a string) or {{\"file\": \"path/to/init.js\"}} "
                         f"(relative to the manifest), or a list of those")
            elif Path(it["file"]).is_absolute():
                p.append(f"{iw}.file: must be relative to the manifest (absolute paths don't travel)")
            else:
                for unknown in placeholders(it["file"]) - known_vars:
                    p.append(f"{iw}.file: unknown variable '{{{unknown}}}'{_suggest(unknown, known_vars)}")
    if "context" in s:
        ctx = s["context"]
        if not isinstance(ctx, dict):
            p.append(f"{w}.context: must be an object of Playwright browser.newContext options, "
                     f"e.g. {{\"reducedMotion\": \"reduce\", \"locale\": \"de-DE\"}}")
            return
        for k in ctx:
            if k in CONTEXT_OWNED:
                p.append(f"{w}.context.{k}: set by the shot's {CONTEXT_OWNED[k]}; set it there instead")


def _dependency_cycles(shots: list[Shot]) -> list[str]:
    graph = {s.raw.get("name"): [d for d in s.raw.get("depends") or [] if isinstance(d, str) and d != s.raw.get("name")]
             for s in shots if isinstance(s.raw.get("depends"), list)}
    problems, done = [], set()

    def visit(n, path):
        if n in path:
            cyc = path[path.index(n):] + [n]
            key = frozenset(cyc)
            if key not in done:
                done.add(key)
                problems.append(f"depends: cycle {' -> '.join(cyc)}; a shot cannot (indirectly) depend on itself")
            return
        for d in graph.get(n, []):
            visit(d, path + [n])
    for n in graph:
        visit(n, [])
    return problems


# Extensions that make an argv entry look like a file the program reads (a script or data).
PATHY_EXTS = {".py", ".sh", ".bash", ".js", ".cjs", ".mjs", ".ts", ".rb", ".pl", ".lua", ".json", ".toml",
              ".yaml", ".yml", ".ini", ".cfg", ".csv", ".txt", ".blend", ".scene", ".jar"}


def lint(m: Manifest) -> list[str]:
    """Warnings for a valid manifest: things that look wrong but may be fine at shoot time.

    A command shot's (or prep's) program that is not on PATH or not at its path, relative
    path-looking argv entries (with a '/' or a script/data extension) that don't exist, inputs
    that match nothing and initScript files that don't exist. Paths a prep declares as outputs
    are never reported (the prep makes them), nor entries with placeholders filled in at run
    time ({out_dir}, {value}, ...)."""
    warnings: list[str] = []
    made = set()
    for s in m.shots:
        if s.prep and isinstance(s.get("outputs"), list):
            made |= {os.path.normpath(str(o)) for o in template(s["outputs"], m.shot_vars(s))}

    def missing(rel: str, base: Path) -> bool:
        path = os.path.normpath(os.path.join(str(base), rel))
        if os.path.exists(path):
            return False
        return os.path.relpath(path, m.dir) not in made

    for s in m.shots:
        if s.get("skip"):
            continue
        w = f"shots[{s.index}] ({s.name})"
        if s.backend == "command" and isinstance(s.get("argv"), list):
            argv = expand_argv(s["argv"], m.shot_vars(s))
            cwd = s.get("cwd")
            if cwd is None:  # relative paths are anchored to the manifest's folder
                base = m.dir
                where = "the manifest's folder"
            else:
                cwd = template(cwd, m.shot_vars(s))
                if placeholders(cwd):
                    base = None  # {work_dir} etc.: only known at run time
                else:
                    base = Path(cwd) if os.path.isabs(cwd) else m.dir / cwd
                    where = f"cwd '{s['cwd']}'"
            for k, a in enumerate(argv):
                if not a or placeholders(a) or a.startswith("-") or "://" in a:
                    continue
                if k == 0 and "/" not in a:
                    if shutil.which(a) is None:
                        warnings.append(f"{w}.argv[0]: program '{a}' is not on PATH")
                    continue
                looks = "/" in a or Path(a).suffix.lower() in PATHY_EXTS
                if not looks:
                    continue
                if os.path.isabs(a):
                    if not os.path.exists(a) and os.path.relpath(a, m.dir) not in made:
                        shown = (os.path.join("{manifest_dir}", os.path.relpath(a, m.dir))
                                 if Path(a).is_relative_to(m.dir) else a)
                        warnings.append(f"{w}.argv[{k}]: '{shown}' does not exist")
                elif base is not None and missing(a, base):
                    warnings.append(f"{w}.argv[{k}]: '{a}' does not exist (relative to {where})")
        for j, pat in enumerate(template(s.get("inputs") or [], m.shot_vars(s))):
            if not isinstance(pat, str) or placeholders(pat):
                continue
            if expand_input(m.dir, pat)[0][1] is None and os.path.normpath(pat) not in made:
                warnings.append(f"{w}.inputs[{j}]: '{pat}' matches no files (yet)")
        for f in init_files(s.get("initScript"), m.shot_vars(s)):
            if not placeholders(f) and not (m.dir / f).is_file():
                warnings.append(f"{w}.initScript: file '{f}' does not exist (relative to the manifest)")
    return warnings
