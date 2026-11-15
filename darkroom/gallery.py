"""Gallery writer: markdown (or a small HTML page) for the shots a manifest has produced."""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

from .manifest import Manifest
from .shoot import State, output_base

START, END = "<!-- darkroom:start -->", "<!-- darkroom:end -->"
# <!-- darkroom:shot NAME --> ... <!-- darkroom:end -->, or self-closing <!-- darkroom:shot NAME /-->
SHOT_BLOCK = re.compile(r"<!--\s*darkroom:shot\s+([A-Za-z0-9][A-Za-z0-9._-]*)\s*(/)?-->"
                        r"(?(2)|.*?<!--\s*darkroom:end\s*-->)", re.S)
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class GalleryError(Exception):
    pass


def project_root(path: Path) -> Path | None:
    """The git work tree containing ``path`` (a ``.git`` dir or file), or None."""
    for d in [path, *path.parents]:
        if (d / ".git").exists():
            return d
    return None


class Linker:
    """Turns output paths into links for a document.

    Links are relative to the document when the document and the media share a root (the git
    work tree holding the manifest, or the manifest's folder outside git). With ``base_url``
    (e.g. ``https://raw.githubusercontent.com/OWNER/REPO/BRANCH/``) every link is that URL plus
    the file's path inside the root, which works anywhere (PR bodies, release notes). A local
    absolute path is never emitted: a document outside the root without ``base_url`` is an error.
    """

    def __init__(self, m: Manifest, doc_dir: Path, base_url: str | None = None):
        self.root = (project_root(m.dir) or m.dir).resolve()
        self.doc_dir = doc_dir.resolve()
        self.base_url = base_url.rstrip("/") + "/" if base_url else None

    def __call__(self, p: Path) -> str:
        p = p.resolve()
        if not p.is_relative_to(self.root):
            raise GalleryError(f"{p.name} is outside the project root {self.root.name}/; "
                               f"write it under the project (see the shot's 'file' / the manifest's 'out')")
        if self.base_url:
            return self.base_url + p.relative_to(self.root).as_posix()
        if not self.doc_dir.is_relative_to(self.root):
            raise GalleryError(f"the document is outside the project root ({self.root.name}/), so a relative "
                               f"link would not resolve; pass --base-url (e.g. "
                               f"https://raw.githubusercontent.com/OWNER/REPO/BRANCH/)")
        return Path(os.path.relpath(p, self.doc_dir)).as_posix()


def _outputs(m: Manifest, state: State, shot) -> list[Path]:
    entry = state.get(shot.name)
    if entry:
        return [m.dir / o for o in entry.get("outputs", []) if (m.dir / o).exists()]
    # No state (e.g. outputs committed, state not): look for the usual names.
    base = output_base(m, shot, None)
    found = []
    for suffix in (".jpg", ".jpeg", ".png", ".gif", ".mp4", ".webm", "-poster.jpg", "-sheet.png"):
        p = base.parent / (base.name + suffix)
        if p.exists():
            found.append(p)
    return found


def entries(m: Manifest, only=None):
    state = State(m)
    for shot in m.shots:
        if only and shot.name not in only:
            continue
        outs = _outputs(m, state, shot)
        if outs:
            yield shot, outs


def classify(outs: list[Path]) -> dict:
    """Sort a shot's outputs into stills, gif, video, poster and sheet by their names."""
    c: dict = {"stills": [], "gif": None, "video": None, "poster": None, "sheet": None}
    for p in outs:
        n = p.name.lower()
        if n.endswith("-poster.jpg"):
            c["poster"] = p
        elif n.endswith("-sheet.png"):
            c["sheet"] = p
        elif n.endswith(".gif"):
            c["gif"] = p
        elif n.endswith((".mp4", ".webm")):
            if c["video"] is None or n.endswith(".mp4"):
                c["video"] = p
        elif n.endswith(IMAGE_EXTS):
            c["stills"].append(p)
    return c


def _values(m: Manifest, shot) -> list[str]:
    """The ``each`` values of a shot, for alt text of its per-value stills."""
    return [str(v) for v in shot.get("each") or []]


def shot_markdown(m: Manifest, shot, outs: list[Path], link) -> list[str]:
    alt = (shot.get("alt") or shot.get("caption") or shot.name).replace("]", ")").replace("\n", " ")
    c = classify(outs)
    video, img = c["video"], c["gif"] or (c["stills"][0] if c["stills"] else None) or c["poster"]
    lines: list[str] = []
    if len(c["stills"]) > 1 and not c["gif"]:
        values = _values(m, shot)
        for k, p in enumerate(c["stills"]):
            lines += [f"![{alt}" + (f" ({values[k]})" if k < len(values) else "") + f"]({link(p)})", ""]
    elif video and not img:
        poster = c["poster"]
        lines += [f"[![{alt}]({link(poster)})]({link(video)})" if poster else f"[{alt}]({link(video)})", ""]
    elif img:
        lines += [f"![{alt}]({link(img)})", ""]
    else:
        return []
    if shot.get("caption"):
        lines += [f"*{shot['caption']}*", ""]
    if video and c["gif"]:
        lines += [f"[{video.suffix[1:].upper()} version]({link(video)})", ""]
    return lines


def markdown(m: Manifest, relative_to: Path | None = None, only=None, heading: str | None = None,
             base_url: str | None = None) -> str:
    link = Linker(m, relative_to.parent if relative_to else m.dir, base_url)
    lines: list[str] = []
    if heading:
        lines += [heading, ""]
    for shot, outs in entries(m, only):
        lines += shot_markdown(m, shot, outs, link)
    return "\n".join(lines).rstrip() + "\n"


def html_page(m: Manifest, relative_to: Path, only=None, title: str = "Media", base_url: str | None = None) -> str:
    link = Linker(m, relative_to.parent, base_url)
    cards = []
    for shot, outs in entries(m, only):
        alt = html.escape(shot.get("alt") or shot.get("caption") or shot.name)
        c = classify(outs)
        if c["video"] and c["video"].suffix == ".mp4":
            poster = c["poster"]
            media = (f'<video controls muted loop playsinline preload="none"'
                     f'{" poster=" + chr(34) + link(poster) + chr(34) if poster else ""}>'
                     f'<source src="{link(c["video"])}" type="video/mp4"></video>')
        else:
            imgs = [c["gif"]] if c["gif"] else c["stills"] or ([c["poster"]] if c["poster"] else [])
            if not imgs:
                continue
            media = "".join(f'<img src="{link(i)}" alt="{alt}" loading="lazy">' for i in imgs)
        cap = html.escape(shot.get("caption", ""))
        cards.append(f'<figure>{media}<figcaption><b>{html.escape(shot.name)}</b> {cap}</figcaption></figure>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#f6f6f4;--fg:#1d1f24;--card:#fff;--muted:#5d6370}}
@media (prefers-color-scheme:dark){{:root{{--bg:#15171c;--fg:#e8e9ec;--card:#1f2229;--muted:#a0a6b2}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
main{{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),1fr));max-width:1400px;margin:auto}}
figure{{margin:0;background:var(--card);border-radius:8px;overflow:hidden}}
img,video{{display:block;width:100%;height:auto}}
figcaption{{padding:8px 12px;color:var(--muted)}} figcaption b{{color:var(--fg)}}
</style></head><body><h1>{html.escape(title)}</h1><main>
{chr(10).join(cards)}
</main></body></html>
"""


def insert(doc: Path, snippet: str) -> bool:
    """Replace the text between <!-- darkroom:start --> and <!-- darkroom:end --> in ``doc``."""
    text = doc.read_text()
    if START not in text or END not in text:
        return False
    new = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: f"{START}\n{snippet}{END}", text, flags=re.S)
    doc.write_text(new)
    return True


def update_doc(m: Manifest, doc: Path, only=None, base_url: str | None = None) -> list[str]:
    """Fill every marker in ``doc``: the whole-gallery block (``darkroom:start``/``end``) and each
    per-shot block (``<!-- darkroom:shot NAME -->...<!-- darkroom:end -->``, or the self-closing
    ``<!-- darkroom:shot NAME /-->``, which is expanded into the open/close form). Returns what
    was updated; raises GalleryError if there are no markers or a marker names an unknown shot."""
    text = doc.read_text()
    link = Linker(m, doc.parent, base_url)
    names = {s.name for s in m.shots}
    unknown = sorted({mm.group(1) for mm in SHOT_BLOCK.finditer(text)} - names)
    if unknown:
        from .manifest import _suggest
        raise GalleryError(f"{doc.name}: marker for unknown shot(s) "
                           + ", ".join(f"'{n}'{_suggest(n, names)}" for n in unknown))
    state = State(m)
    done: list[str] = []

    def shot_block(mm: re.Match) -> str:
        name = mm.group(1)
        if only and name not in only:
            return mm.group(0)
        shot = m.shot(name)
        outs = _outputs(m, state, shot)
        if not outs:
            return mm.group(0)  # nothing shot yet: leave the block as it is
        body = "\n".join(shot_markdown(m, shot, outs, link)).rstrip()
        done.append(name)
        return f"<!-- darkroom:shot {name} -->\n{body}\n<!-- darkroom:end -->"

    # Whole-gallery block first (its END is the first darkroom:end after START), then shots.
    found = False
    if START in text and END in text[text.index(START):]:
        found = True
        snippet = markdown(m, doc, only, base_url=base_url)
        text = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: f"{START}\n{snippet}{END}",
                      text, count=1, flags=re.S)
        done.append("gallery")
    text, n = SHOT_BLOCK.subn(shot_block, text)
    found = found or n > 0
    if not found:
        raise GalleryError(f"{doc.name}: no markers; add {START} ... {END} for the whole gallery, or "
                           f"<!-- darkroom:shot NAME --> ... {END} (or <!-- darkroom:shot NAME /-->) per shot")
    doc.write_text(text)
    return done
