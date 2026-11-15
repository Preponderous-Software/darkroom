"""darkroom command line."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import encode as enc
from . import gallery
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
    failed = shoot(m, only=args.only, tag=args.tag, retries=args.retries, force=args.force,
                   encode_only=args.encode)
    if failed:
        print(f"{failed} shot(s) failed", file=sys.stderr)
    return 1 if failed else 0


def cmd_gallery(args):
    m = _load(args)
    rel = Path(args.relative_to).resolve() if args.relative_to else None
    try:
        if args.format == "html":
            target = Path(args.write or (m.out_dir / "index.html")).resolve()
            page = gallery.html_page(m, target, args.only, args.title, base_url=args.base_url)
            target.write_text(page)
            print(target)
            return 0
        if args.insert:
            done = gallery.update_doc(m, Path(args.insert), args.only, base_url=args.base_url)
            print(f"updated {args.insert}: {', '.join(done) if done else 'nothing shot yet'}")
            return 0
        text = gallery.markdown(m, rel or (Path(args.write).resolve() if args.write else None), args.only,
                                base_url=args.base_url)
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
    p.set_defaults(fn=cmd_shoot)

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
