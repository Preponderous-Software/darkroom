"""darkroom command line."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import encode as enc
from . import gallery
from . import procs
from .manifest import ManifestError, lint, load
from .shoot import base_name, load_frames, make_pair, sheet_labels, shoot


def _vars(pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--var {p}: expected NAME=VALUE")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _positive(v: str) -> int:
    try:
        n = int(v)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of 1 or more (got '{v}')")
    return n


def _load(args):
    try:
        return load(args.manifest, _vars(getattr(args, "var", None)))
    except ManifestError as e:
        print(f"{args.manifest}: invalid manifest", file=sys.stderr)
        for p in e.problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(2)


def cmd_check(args):
    m = _load(args)
    kinds = {}
    for s in m.shots:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    print(f"{args.manifest}: OK, {len(m.shots)} shot(s) ("
          + ", ".join(f"{n} {k}" for k, n in kinds.items()) + f"); out: {m.out_dir}")
    # Warnings, not errors: paths can be made by a prep or depend on --var.
    for w in lint(m):
        print(f"  warning: {w}", file=sys.stderr)
    return 0


def cmd_shoot(args):
    m = _load(args)
    if args.out:
        m.out_override = Path(args.out).resolve()
    with procs.run_scope(args.manifest) as run:
        others = [r["id"] for r in procs.find(manifest=args.manifest) if r["id"] != run.id and r["alive"]]
        if others:
            print(f"warning: another darkroom run is shooting {args.manifest} ({', '.join(others)}); "
                  "takes of the same shot will collide", file=sys.stderr)
        try:
            if args.adopt:
                from .shoot import adopt
                adopt(m, only=args.only, tag=args.tag)
                return 0
            failed = shoot(m, only=args.only, tag=args.tag, retries=args.retries, force=args.force,
                           encode_only=args.encode, jobs=args.jobs)
        except (KeyboardInterrupt, procs.Stopped) as e:
            why = "interrupted" if isinstance(e, KeyboardInterrupt) else f"stopped ({e})"
            print(f"darkroom run {run.id} {why}; its processes were stopped", file=sys.stderr)
            return 130 if isinstance(e, KeyboardInterrupt) else 143
    if failed:
        print(f"{failed} shot(s) failed", file=sys.stderr)
    return 1 if failed else 0


def cmd_gallery(args):
    m = _load(args)
    rel = Path(args.relative_to).resolve() if args.relative_to else None
    layout = gallery.Layout.of(m, args.layout, args.by)
    try:
        if args.format == "html":
            target = Path(args.write or (m.out_dir / "index.html")).resolve()
            page = gallery.html_page(m, target, args.only, args.title, base_url=args.base_url, layout=layout)
            target.write_text(page)
            print(target)
            return 0
        if args.insert:
            done = gallery.update_doc(m, Path(args.insert), args.only, base_url=args.base_url, layout=layout)
            print(f"updated {args.insert}: {', '.join(done) if done else 'nothing shot yet'}")
            return 0
        text = gallery.markdown(m, rel or (Path(args.write).resolve() if args.write else None), args.only,
                                base_url=args.base_url, layout=layout)
    except gallery.GalleryError as e:
        print(f"gallery: {e}", file=sys.stderr)
        return 2
    if args.write:
        Path(args.write).write_text(text)
        print(args.write)
    else:
        sys.stdout.write(text)
    return 0


def cmd_pair(args):
    m = _load(args)
    shot = m.shot(args.name)
    if shot.kind != "pair":
        from .manifest import Shot
        shot = Shot({"name": f"{shot.name}-pair", "kind": "pair", "of": shot.name,
                     "labels": args.labels or ["before", "after"]}, -1)
    make_pair(m, shot)
    return 0


def _age(started: str | None) -> str:
    import datetime as dt
    try:
        secs = int((dt.datetime.now() - dt.datetime.fromisoformat(started)).total_seconds())
    except (TypeError, ValueError):
        return "?"
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m" if secs >= 3600 else f"{secs // 60}m{secs % 60:02d}s"


def cmd_ps(args):
    rs = procs.runs()
    if args.json:
        print(json.dumps(rs, indent=2))
        return 0
    if not rs:
        print("no darkroom runs")
        return 0
    print(f"{'RUN':<8} {'PID':>7} {'AGE':>7}  {'SHOT':<24} MANIFEST")
    for r in rs:
        status = "" if r["alive"] else f"  [darkroom gone; {len(r['live_groups'])} group(s) left: darkroom stop {r['id']}]"
        print(f"{r['id']:<8} {r['pid']:>7} {_age(r.get('started')):>7}  {str(r.get('shot') or '-'):<24} "
              f"{r.get('manifest') or '-'}{status}")
    return 0


def cmd_stop(args):
    if bool(args.run_id) == bool(args.manifest):
        print("stop: name one run: darkroom stop RUN-ID, or darkroom stop --manifest PATH "
              "(darkroom ps lists them)", file=sys.stderr)
        return 2
    rs = procs.find(args.run_id, args.manifest)
    if not rs:
        print(f"stop: no live darkroom run {'for ' + args.manifest if args.manifest else repr(args.run_id)} "
              "(darkroom ps lists them)", file=sys.stderr)
        return 1
    if len(rs) > 1 and not args.all:
        print(f"stop: {len(rs)} runs match: {', '.join(r['id'] for r in rs)}; name one by its id, "
              "or pass --all", file=sys.stderr)
        return 2
    for r in rs:
        done = procs.stop(r, timeout=args.timeout)
        print(f"stopped {r['id']}: " + ("; ".join(done) if done else "nothing was left running"))
    return 0


def _take_manifest(take: Path) -> tuple[Path | None, str | None]:
    """For a take folder (``.darkroom/takes/KEY``): the project dir and the manifest's ``out``."""
    if not ((take / "meta.json").exists() and take.parent.name == "takes" and take.parent.parent.name == ".darkroom"):
        return None, None
    project = take.parent.parent.parent
    try:
        meta = json.loads((take / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        meta = {}
    out = meta.get("out")
    if out is None and (project / "media.json").exists():  # takes made before meta recorded it
        try:
            out = json.loads((project / "media.json").read_text()).get("out", "media")
        except (OSError, json.JSONDecodeError):
            pass
    return project, out


def _takes_hint(path: Path, depth: int = 4) -> str:
    """Where takes named like PATH really are: takes live in ``.darkroom/takes`` beside each
    manifest (e.g. ``tools/capture/.darkroom/takes/X``), not necessarily at the repo root."""
    name = path.name
    found: list[str] = []
    for d in range(depth + 1):
        pattern = "/".join(["*"] * d + [".darkroom", "takes", name or "*"])
        found += [str(p) for p in Path(".").glob(pattern) if p.is_dir()]
        if len(found) >= 5:
            break
    if found:
        return ("; takes live beside their manifest, in .darkroom/takes. Found: " + ", ".join(sorted(found)[:5])
                + " (or run: darkroom sheet MANIFEST SHOT)")
    return ("; takes live beside their manifest, in <manifest dir>/.darkroom/takes/<shot>, and none named "
            f"'{name}' are under this folder; try: darkroom sheet MANIFEST SHOT")


def cmd_sheet(args):
    labels = args.labels
    if args.manifest.endswith(".json"):
        m = _load(args)
        if not args.name:
            print("sheet: name the shot whose take to sheet (darkroom sheet media.json NAME)", file=sys.stderr)
            return 2
        shot = m.shot(args.name)
        key = base_name(shot, args.tag)
        try:
            frames, meta = load_frames(m, key, shot)
        except FileNotFoundError:
            print(f"sheet: no take of '{key}' yet; shoot it first", file=sys.stderr)
            return 1
        out = Path(args.out) if args.out else m.out_dir / f"{key}-sheet.png"
    else:  # a take folder, or any folder of images
        folder = Path(args.manifest).resolve()
        if not folder.is_dir():
            print(f"sheet: {args.manifest}: no such folder" + _takes_hint(Path(args.manifest)), file=sys.stderr)
            return 2
        project, out_rel = _take_manifest(folder)
        meta = {}
        if project is not None:
            meta = json.loads((folder / "meta.json").read_text())
            files = [folder / "frames" / f for f in meta.get("frames", [])]
            default_out = (project / out_rel if out_rel else project) / f"{folder.name}-sheet.png"
        else:
            files = sorted(p for p in folder.iterdir()
                           if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
                           and not (p.name == "sheet.png" or p.stem.endswith("-sheet")))
            default_out = folder.parent / f"{folder.name}-sheet.png"
        if not files:
            print(f"sheet: no images in {args.manifest}", file=sys.stderr)
            return 1
        frames = [enc.load_rgb(p) for p in files]
        out = Path(args.out) if args.out else default_out
    r = enc.contact_sheet(frames, out, cols=args.cols, thumb_width=args.thumb,
                          labels=sheet_labels(labels, meta), width=args.width)
    print(f"{out}: {r.describe().split(': ', 1)[1]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="darkroom", description="Stills, GIFs, videos, contact sheets and "
                                 "before/after pairs from a JSON shot list.")
    ap.add_argument("--version", action="version", version=f"darkroom {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, vars_=True):
        p.add_argument("manifest", help="media.json")
        if vars_:
            p.add_argument("--var", action="append", metavar="NAME=VALUE", help="override a manifest var")

    p = sub.add_parser("check", help="validate a manifest")
    common(p)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("shoot", help="capture and encode shots (skips up-to-date ones)")
    common(p)
    p.add_argument("--only", nargs="+", action="extend", metavar="NAME")
    p.add_argument("--tag", help="label this run, e.g. before / after (outputs NAME.TAG.*)")
    p.add_argument("--retries", type=int, help="attempts after the first (default: the shot's, else 2)")
    p.add_argument("--force", action="store_true", help="reshoot even if up to date")
    p.add_argument("--encode", action="store_true", help="re-encode the last takes without reshooting")
    p.add_argument("--out", metavar="DIR", help="write every output (including 'file' ones) into DIR "
                   "instead, e.g. a scratch dir for review; the takes are shared")
    p.add_argument("--adopt", action="store_true", help="shoot nothing: record shots whose outputs all "
                   "exist (e.g. committed, in a fresh checkout) as up to date for their current hashes")
    p.add_argument("--jobs", "-j", type=_positive, default=1, metavar="N",
                   help="run up to N independent shots at once (default 1); shots with the same lock "
                   "never overlap, dependents wait, and one session's shots run in order")
    p.set_defaults(fn=cmd_shoot)

    p = sub.add_parser("ps", help="list this user's running darkroom shoots (stale entries are pruned)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ps)

    p = sub.add_parser("stop", help="stop one darkroom run and every process it started (never pkill -f)")
    p.add_argument("run_id", nargs="?", metavar="RUN-ID", help="as darkroom ps shows it (a unique prefix will do)")
    p.add_argument("--manifest", metavar="PATH", help="the run shooting this manifest")
    p.add_argument("--all", action="store_true", help="with --manifest: stop every run of it")
    p.add_argument("--timeout", type=float, default=15, help="seconds to let it clean up before killing (15)")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("gallery", help="markdown (or HTML) for the outputs")
    common(p)
    p.add_argument("--format", choices=["md", "html"], default="md")
    p.add_argument("--relative-to", metavar="DOC", help="make paths relative to this document (e.g. README.md)")
    p.add_argument("--write", metavar="FILE", help="write to FILE instead of stdout")
    p.add_argument("--insert", metavar="DOC", help="fill the darkroom markers in DOC (the darkroom:start/end "
                   "block and/or per-shot darkroom:shot NAME blocks)")
    p.add_argument("--base-url", metavar="URL", help="link media as URL + path in the repo (e.g. "
                   "https://raw.githubusercontent.com/OWNER/REPO/BRANCH/), for PR bodies and other docs "
                   "outside the repo")
    p.add_argument("--only", nargs="+", action="extend", metavar="NAME")
    p.add_argument("--title", default="Media")
    p.add_argument("--layout", choices=["list", "table"], help="list (default: one shot after another) or "
                   "table (rows of screens, one column per device); the manifest's gallery.layout is the default")
    p.add_argument("--by", choices=["device"], help="the table's columns (device)")
    p.set_defaults(fn=cmd_gallery)

    p = sub.add_parser("pair", help="compose before/after takes of a shot side by side")
    common(p)
    p.add_argument("name")
    p.add_argument("--labels", nargs=2)
    p.set_defaults(fn=cmd_pair)

    p = sub.add_parser("sheet", help="contact sheet of a clip's frames (or of a folder of images)")
    p.add_argument("manifest", help="media.json, or a folder of images")
    p.add_argument("name", nargs="?")
    p.add_argument("--tag")
    p.add_argument("-o", "--out", metavar="FILE", help="where to write the sheet (default: the manifest's out "
                   "dir as NAME-sheet.png; for a plain folder, FOLDER-sheet.png beside it)")
    p.add_argument("--cols", type=int, default=4)
    p.add_argument("--width", type=int, default=enc.SHEET_WIDTH, help="about how wide the sheet is (default 1600)")
    p.add_argument("--thumb", type=int, help="fixed tile width instead of --width")
    p.add_argument("--labels", choices=["none", "index", "caption"], default="index",
                   help="label tiles with the frame index (default), the 'each' value, or nothing")
    p.add_argument("--var", action="append")
    p.set_defaults(fn=cmd_sheet)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyError as e:
        print(e.args[0] if e.args else e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
