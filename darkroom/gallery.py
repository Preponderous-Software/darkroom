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
# Any one marker: start, end, or a shot opener (group 1 = name, group 2 = "/" if self-closing).
MARKER = re.compile(r"<!--\s*darkroom:(?:start|end|shot\s+([A-Za-z0-9][A-Za-z0-9._-]*)\s*(/)?)\s*-->")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def code_regions(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of fenced code blocks and inline code spans in markdown, where a
    marker is documentation, not a marker."""
    regions: list[tuple[int, int]] = []
    pos, fence, fence_start = 0, None, 0
    prose: list[tuple[int, int]] = []
    for line in text.splitlines(keepends=True):
        m = FENCE.match(line)
        if fence is None and m:
            fence, fence_start = m.group(1), pos
        elif fence is not None and m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                and not line.strip().strip(fence[0]):
            regions.append((fence_start, pos + len(line)))
            fence = None
        elif fence is None:
            prose.append((pos, pos + len(line)))
        pos += len(line)
    if fence is not None:  # an unclosed fence runs to the end of the document
        regions.append((fence_start, len(text)))
    # Inline code: a run of N backticks up to the next run of exactly N (within prose, which
    # may span lines of one paragraph).
    for a, b in _merge(prose):
        chunk = text[a:b]
        i = 0
        while True:
            m = re.compile(r"`+").search(chunk, i)
            if not m:
                break
            n = len(m.group(0))
            close = re.compile(r"(?<!`)`{%d}(?!`)" % n).search(chunk, m.end())
            if not close:
                i = m.end()
                continue
            regions.append((a + m.start(), a + close.end()))
            i = close.end()
    return regions


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in spans:
        if out and out[-1][1] == a:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def find_blocks(text: str) -> list[tuple[int, int, str | None]]:
    """Marker blocks outside code: (start, end, shot name or None for the whole gallery).
    A shot opener or start marker runs to the next end marker; a self-closing shot marker is a
    block by itself. Markers inside code spans or fences are ignored."""
    regions = code_regions(text)

    def in_code(i: int) -> bool:
        return any(a <= i < b for a, b in regions)
    tokens = [m for m in MARKER.finditer(text) if not in_code(m.start())]
    blocks, k = [], 0
    while k < len(tokens):
        t = tokens[k]
        body = t.group(0)
        if "darkroom:end" in body:
            k += 1
            continue
        name = t.group(1)
        if name and t.group(2):
            blocks.append((t.start(), t.end(), name))
            k += 1
            continue
        end = next((j for j in range(k + 1, len(tokens)) if "darkroom:end" in tokens[j].group(0)), None)
        if end is None:
            break
        blocks.append((t.start(), tokens[end].end(), name))
        k = end + 1
    return blocks


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
    if shot.prep:
        return []  # a prep's outputs are data files, not media
    entry = state.get(shot.name)
    if entry:
        return [m.dir / o for o in entry.get("outputs", []) if (m.dir / o).exists()]
    # No state (e.g. outputs committed, state not): look for the usual names.
    base = output_base(m, shot, None)
    found = [p for p, _, _ in named_stills(m, shot, [s.with_suffix(e) for s in _named_stems(m, shot).values()
                                                     for e in (".jpg", ".png")])]
    if found:
        return found
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


def gallery_options(shot) -> dict:
    g = shot.get("gallery")
    return g if isinstance(g, dict) else {}


def _named_stems(m: Manifest, shot) -> dict[str, Path]:
    """{key: output path without its extension} for a command still's named outputs."""
    base = output_base(m, shot, None)
    templated = "{key}" in (shot.get("file") or "")
    return {key: (output_base(m, shot, None, {"key": key}) if templated else base.parent / f"{base.name}-{key}")
            for key, _ in shot.named_outputs()}


def named_stills(m: Manifest, shot, stills: list[Path]) -> list[tuple[Path, str, dict]]:
    """(path, key, output entry) for each named output among ``stills``, in ``outputs`` order."""
    by_stem = {p.with_suffix("").resolve(): p for p in stills if p.exists()}
    entries = dict(shot.named_outputs())
    out = []
    for key, stem in _named_stems(m, shot).items():
        p = by_stem.get(stem.resolve())
        if p is not None:
            out.append((p, key, entries[key]))
    return out


def _output_alt(o: dict, shot_alt: str, key: str) -> str:
    text = o.get("alt") or o.get("caption")
    return text.replace("]", ")").replace("\n", " ") if text else f"{shot_alt} ({key})"


def shot_markdown(m: Manifest, shot, outs: list[Path], link) -> list[str]:
    alt = (shot.get("alt") or shot.get("caption") or shot.name).replace("]", ")").replace("\n", " ")
    c = classify(outs)
    opts = gallery_options(shot)
    width = opts.get("width")

    def image(p: Path, text: str) -> str:
        # A width needs HTML; GitHub and most renderers keep <img> in markdown.
        if width:
            return f'<img src="{html.escape(link(p))}" alt="{html.escape(text)}" width="{int(width)}">'
        return f"![{text}]({link(p)})"
    # a clip with video and no GIF shows its poster linked to the video (below), so the poster is
    # the main image only when there is no video
    video = c["video"]
    img = c["gif"] or (c["stills"][0] if c["stills"] else None) or (None if video else c["poster"])
    lines: list[str] = []
    named = named_stills(m, shot, c["stills"]) if shot.named_outputs() else []
    if named:
        # Named outputs: each image with its own alt and caption, then the shot's caption.
        for p, key, o in named:
            lines += [image(p, _output_alt(o, alt, key)), ""]
            if o.get("caption"):
                lines += [f"*{o['caption']}*", ""]
    elif len(c["stills"]) > 1 and not c["gif"]:
        values = _values(m, shot) or [p.stem for p in c["stills"]]
        for k, p in enumerate(c["stills"]):
            lines += [image(p, alt + (f" ({values[k]})" if k < len(values) else "")), ""]
    elif video and not img:
        poster = c["poster"]
        lines += [f"[{image(poster, alt)}]({link(video)})" if poster else f"[{alt}]({link(video)})", ""]
    elif img:
        if c["gif"] and opts.get("poster") and c["poster"]:
            # The poster beside the GIF: two images in one paragraph sit side by side.
            lines += [image(c["poster"], f"{alt} (poster)") + " " + image(img, alt), ""]
        else:
            lines += [image(img, alt), ""]
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
        named = named_stills(m, shot, c["stills"]) if shot.named_outputs() else []
        if named:
            # One card per named output, with its own alt and caption.
            w = gallery_options(shot).get("width")
            size = f' width="{int(w)}" style="width:{int(w)}px;max-width:100%"' if w else ""
            for p, key, o in named:
                a = html.escape(o.get("alt") or o.get("caption") or f"{shot.get('alt') or shot.get('caption') or shot.name} ({key})")
                cap = html.escape(o.get("caption") or shot.get("caption", ""))
                cards.append(f'<figure><img src="{link(p)}" alt="{a}" loading="lazy"{size}><figcaption>'
                             f'<b>{html.escape(shot.name)}-{html.escape(key)}</b> {cap}</figcaption></figure>')
            continue
        if c["video"] and c["video"].suffix == ".mp4":
            poster = c["poster"]
            media = (f'<video controls muted loop playsinline preload="none"'
                     f'{" poster=" + chr(34) + link(poster) + chr(34) if poster else ""}>'
                     f'<source src="{link(c["video"])}" type="video/mp4"></video>')
        else:
            imgs = [c["gif"]] if c["gif"] else c["stills"] or ([c["poster"]] if c["poster"] else [])
            if c["gif"] and c["poster"] and gallery_options(shot).get("poster"):
                imgs = [c["poster"], c["gif"]]
            if not imgs:
                continue
            w = gallery_options(shot).get("width")
            size = f' width="{int(w)}" style="width:{int(w)}px;max-width:100%"' if w else ""
            media = "".join(f'<img src="{link(i)}" alt="{alt}" loading="lazy"{size}>' for i in imgs)
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
    blocks = find_blocks(text)
    unknown = sorted({name for _, _, name in blocks if name} - names)
    if unknown:
        from .manifest import _suggest
        raise GalleryError(f"{doc.name}: marker for unknown shot(s) "
                           + ", ".join(f"'{n}'{_suggest(n, names)}" for n in unknown))
    state = State(m)
    done: list[str] = []

    def shot_block(name: str, original: str) -> str:
        if only and name not in only:
            return original
        shot = m.shot(name)
        outs = _outputs(m, state, shot)
        if not outs:
            return original  # nothing shot yet: leave the block as it is
        body = "\n".join(shot_markdown(m, shot, outs, link)).rstrip()
        done.append(name)
        return f"<!-- darkroom:shot {name} -->\n{body}\n<!-- darkroom:end -->"

    # Markers inside code spans and fenced blocks are documentation and are left alone.
    found = bool(blocks)
    pieces, pos, gallery_done = [], 0, False
    for a, b, name in blocks:
        pieces.append(text[pos:a])
        if name is None:
            if gallery_done:
                pieces.append(text[a:b])
            else:
                snippet = markdown(m, doc, only, base_url=base_url)
                pieces.append(f"{START}\n{snippet}{END}")
                done.append("gallery")
                gallery_done = True
        else:
            pieces.append(shot_block(name, text[a:b]))
        pos = b
    pieces.append(text[pos:])
    text = "".join(pieces)
    if not found:
        raise GalleryError(f"{doc.name}: no markers; add {START} ... {END} for the whole gallery, or "
                           f"<!-- darkroom:shot NAME --> ... {END} (or <!-- darkroom:shot NAME /-->) per shot")
    doc.write_text(text)
    return done
