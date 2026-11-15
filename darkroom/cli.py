"""darkroom command line."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import encode as enc
from . import gallery
from .manifest import ManifestError, load
from .shoot import base_name, load_frames, make_pair, shoot, take_dir


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
    return 0


def cmd_shoot(args):
    m = _load(args)
    failed = shoot(m, only=args.only, tag=args.tag, retries=args.retries, force=args.force,
                   encode_only=args.encode)
    if failed:
        print(f"{failed} shot(s) failed", file=sys.stderr)
    return 1 if failed else 0


def cmd_gallery(args):
    m = _load(args)
    rel = Path(args.relative_to).resolve() if args.relative_to else None
    if args.format == "html":
        target = Path(args.write or (m.out_dir / "index.html")).resolve()
        target.write_text(gallery.html_page(m, target, args.only, args.title))
        print(target)
        return 0
    text = gallery.markdown(m, rel or (Path(args.insert).resolve() if args.insert else None), args.only)
    if args.insert:
        if not gallery.insert(Path(args.insert), text):
            print(f"{args.insert}: no {gallery.START} ... {gallery.END} markers", file=sys.stderr)
            return 1
        print(f"updated {args.insert}")
    elif args.write:
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


def cmd_sheet(args):
    if args.manifest.endswith(".json"):
        m = _load(args)
        shot = m.shot(args.name)
        key = base_name(shot, args.tag)
        frames, _ = load_frames(m, key, shot)
        out = Path(args.out) if args.out else m.out_dir / f"{key}-sheet.png"
    else:  # a folder of images
        files = sorted(p for p in Path(args.manifest).iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        frames = [enc.load_rgb(p) for p in files]
        out = Path(args.out or Path(args.manifest) / "sheet.png")
    r = enc.contact_sheet(frames, out, cols=args.cols, thumb_width=args.thumb)
    print(r.describe())
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
    p.add_argument("--only", nargs="+", metavar="NAME")
    p.add_argument("--tag", help="label this run, e.g. before / after (outputs NAME.TAG.*)")
    p.add_argument("--retries", type=int, help="attempts after the first (default: the shot's, else 2)")
    p.add_argument("--force", action="store_true", help="reshoot even if up to date")
    p.add_argument("--encode", action="store_true", help="re-encode the last takes without reshooting")
    p.set_defaults(fn=cmd_shoot)

    p = sub.add_parser("gallery", help="markdown (or HTML) for the outputs")
    common(p)
    p.add_argument("--format", choices=["md", "html"], default="md")
    p.add_argument("--relative-to", metavar="DOC", help="make paths relative to this document (e.g. README.md)")
    p.add_argument("--write", metavar="FILE", help="write to FILE instead of stdout")
    p.add_argument("--insert", metavar="DOC", help="replace the darkroom:start/end block in DOC")
    p.add_argument("--only", nargs="+", metavar="NAME")
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
    p.add_argument("--out")
    p.add_argument("--cols", type=int, default=4)
    p.add_argument("--thumb", type=int, default=320)
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
