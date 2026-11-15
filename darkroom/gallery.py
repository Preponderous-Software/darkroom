"""Gallery writer: markdown (or a small HTML page) for the shots a manifest has produced."""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

from .manifest import Manifest
from .shoot import State

START, END = "<!-- darkroom:start -->", "<!-- darkroom:end -->"


def _outputs(m: Manifest, state: State, shot) -> list[Path]:
    entry = state.get(shot.name)
    if entry:
        return [m.dir / o for o in entry.get("outputs", []) if (m.dir / o).exists()]
    # No state (e.g. outputs committed, state not): look for the usual names.
    found = []
    for suffix in (".jpg", ".png", ".gif", ".mp4", ".webm", "-poster.jpg", "-sheet.png"):
        p = m.out_dir / f"{shot.name}{suffix}"
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


def _link(p: Path, base: Path) -> str:
    return Path(os.path.relpath(p.resolve(), base.resolve())).as_posix()


def markdown(m: Manifest, relative_to: Path | None = None, only=None, heading: str | None = None) -> str:
    base = (relative_to.parent if relative_to else m.dir)
    lines: list[str] = []
    if heading:
        lines += [heading, ""]
    for shot, outs in entries(m, only):
        alt = (shot.get("alt") or shot.get("caption") or shot.name).replace("]", ")").replace("\n", " ")
        by = {p.name[len(shot.name):]: p for p in outs}
        video = by.get(".mp4") or by.get(".webm")
        img = by.get(".gif") or by.get(".jpg") or by.get(".png") or by.get("-poster.jpg")
        if video and not img:
            poster = by.get("-poster.jpg")
            img_md = f"[![{alt}]({_link(poster, base)})]({_link(video, base)})" if poster else f"[{alt}]({_link(video, base)})"
        elif img:
            img_md = f"![{alt}]({_link(img, base)})"
        else:
            continue
        lines.append(img_md)
        if shot.get("caption"):
            lines += ["", f"*{shot['caption']}*"]
        if video and img and by.get(".gif") is img:
            lines += ["", f"[{video.suffix[1:].upper()} version]({_link(video, base)})"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def html_page(m: Manifest, relative_to: Path, only=None, title: str = "Media") -> str:
    base = relative_to.parent
    cards = []
    for shot, outs in entries(m, only):
        alt = html.escape(shot.get("alt") or shot.get("caption") or shot.name)
        by = {p.name[len(shot.name):]: p for p in outs}
        if by.get(".mp4"):
            poster = by.get("-poster.jpg")
            media = (f'<video controls muted loop playsinline preload="none"'
                     f'{" poster=" + chr(34) + _link(poster, base) + chr(34) if poster else ""}>'
                     f'<source src="{_link(by[".mp4"], base)}" type="video/mp4"></video>')
        else:
            img = by.get(".gif") or by.get(".jpg") or by.get(".png") or by.get("-poster.jpg")
            if not img:
                continue
            media = f'<img src="{_link(img, base)}" alt="{alt}" loading="lazy">'
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
