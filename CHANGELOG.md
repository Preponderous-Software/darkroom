# Changelog

## 0.2.0 (2026-11-15)

From the first pilot: a native desktop game captured through the `command` backend under Xvfb.

### Changed

- GIFs default to the `adaptive` palette. GIFs are no longer run through Pillow's optimizer, which could leave colour indices past a frame's trimmed palette, making GIFs that ImageMagick rejects ("invalid colormap index").

- **`command` shots run in a scratch directory.** The default `cwd` is now `{work_dir}`, a
  fresh directory per run beside `{out_dir}` (deleted afterwards), not the manifest's
  directory, so files a program writes into its working directory (an `imgui.ini`, logs,
  caches) no longer land in the project. Relative paths in `argv` that pointed next to the
  manifest keep working: the program when it has a slash (`./build/Game`), and any entry
  naming an existing file there (`render.py`) or an existing path with a slash. Paths
  inside flags (`--scene=scenes/a.json`) and in `env` are not rewritten: write them as
  `{manifest_dir}/...`, or set `"cwd": "{manifest_dir}"` to get 0.1's behaviour back. A relative `cwd` is relative to the manifest (it was relative to wherever
  darkroom was run).
- **Gallery links never leave the project.** Links are relative to the document only when the
  document and the media share a root (the git work tree holding the manifest, or the
  manifest's folder). Otherwise `gallery` needs `--base-url` and exits 2 with a pointed
  message, instead of writing `../../../..` paths.
- **Contact sheets** size tiles from the frames to about 1600 px wide (was 320 px tiles) and
  never upscale. `darkroom sheet` on a take folder reads its `frames/` (it used to sheet the
  take's own `sheet.png`, giving a tiny one-tile sheet labelled `#0`) and writes to the
  manifest's out dir, not into the take; a plain folder's sheet goes beside it. Other sheets
  in a folder are not treated as frames.
- `gallery --write FILE` makes links relative to `FILE`.

### Added

- `each`: a list of values on a `command` or `web` shot, one run per value with `{value}`,
  `{i}` and `{n}`. A still with `each` writes one image per value. Takes record each frame's
  run and value (`meta.json` `runs`, `captions`).
- List vars, spliced into `argv` as a whole `"@name"` entry; `--var` on a list var is split
  like a shell.
- `KEY+` (`argv+`, `steps+`, `setup+`, `block+`, `launchArgs+`) extends the default's list
  instead of replacing it.
- `file`: a per-shot output path (relative to the manifest) whose extension picks the
  format, to replace an existing image in place.
- `kind: "grid"`: a captioned composite with an optional title and no frame numbers, from
  another shot's take (`of`, `frames`) or a list of `tiles` (shots or image files).
- Contact-sheet labels: `none`, `index` (default) or `caption` (the `each` value), as
  `sheet: { "labels", "cols", "width" }` on a clip and `--labels` on `darkroom sheet`;
  `darkroom sheet -o FILE` and `--width`.
- `gallery --base-url URL` (e.g. `https://raw.githubusercontent.com/OWNER/REPO/BRANCH/`) for
  PR bodies and other documents outside the repo.
- Per-shot doc markers, `<!-- darkroom:shot NAME -->...<!-- darkroom:end -->` or the
  self-closing `<!-- darkroom:shot NAME /-->`, filled by `gallery --insert` alongside the
  whole-gallery block.
- `{work_dir}` for `command` shots.

## 0.1.0 (2026-11-15)

First version.

- `xvfb: true` runs Xvfb and the program with software OpenGL (llvmpipe), so GLX keeps working where a
  host GPU driver would crash Xvfb (e.g. NVIDIA under WSL); `xvfb.env` overrides it.

- `darkroom shoot | check | gallery | pair | sheet` over a JSON shot list (`media.json`).
- Shot kinds: `still`, `clip`, `pair` (before/after, from two tagged runs or two image paths).
- Backends: `web` (Playwright in a bundled Node runner; adapters `strata-kit` and `hook`) and
  `command` (any program that writes images; optional private Xvfb display per capture).
- Steps: `js`, `key`, `type`, `tap`, `click`, `wait`, `waitFor`, `frames`, `camera`, `css`,
  `capture`, `mark`, `repeat`.
- Devices: `desktop`, `hd`, `phone` (390x844, DPR 3, touch), `landscape-phone`, or custom.
- Encoders: budget-fitted JPEG/PNG stills, GIF (octree / median / adaptive palettes, dither,
  budget fitting), MP4 (H.264) and WebM (VP9) with a CRF ladder, posters, contact sheets,
  captioned side-by-side pairs.
- Resilience: per-shot retries with a fresh browser or process, resume from
  `.darkroom/state.json`, `--encode` to re-encode a saved take, named file locks.
- Gallery: markdown (relative to any document, or inserted between markers) and an HTML page.
