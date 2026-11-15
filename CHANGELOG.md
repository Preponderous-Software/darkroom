# Changelog

## 0.5.0 (2026-11-15)

Manifests, outputs and capture hashes are unchanged: nothing is reshot on upgrade.

### Added

- **Run-scoped process control.** Every process darkroom starts (the Node runner and the
  Chromium it launches, `command` programs, Xvfb, ffmpeg) runs in a new session, so it leads
  its own process group. Each `darkroom shoot` registers itself (pid, process groups,
  manifest, cwd, start time, current shot) in `$DARKROOM_RUN_DIR`, else
  `$XDG_RUNTIME_DIR/darkroom-runs`, else `/tmp/darkroom-runs-<uid>`, and on exit, an error,
  Ctrl-C, SIGTERM or SIGHUP kills only its own groups, including descendants that started a
  session of their own.
- **`darkroom ps`** lists live runs (id, pid, age, current shot, manifest; `--json` too) and
  prunes entries whose run is gone.
- **`darkroom stop RUN-ID`** / **`darkroom stop --manifest PATH`** stops exactly one run:
  SIGTERM, a grace period (`--timeout`, 15 s), then SIGKILL for whatever of its groups is
  left. Pids are checked against their start time, so a recycled pid is never signalled. Use
  it instead of `pkill -f`, which kills other projects' captures too.
- **`lockTimeout`** (seconds) on shots and preps: give up waiting for a busy `lock` and fail
  the attempt. The wait message now names the pid that holds the lock.
- A warning when another run is already shooting the same manifest.

### Fixed

- A timed-out command or browser run could hang forever: `subprocess.run` killed only the
  direct child, then waited for output pipes still held by its children (Chromium, a game's
  helper). The whole tree is now killed and the drain after the kill is bounded.
- Chromium, Xvfb and programs' helpers were left running after Ctrl-C, SIGTERM or a timeout.
- A helper a `command` program forked and left running is killed when the program exits.
- `.darkroom/state.json` is updated under a file lock (re-read, change one entry, write a temp
  file and rename), so parallel `--only A` and `--only B` runs on one manifest no longer lose
  each other's entries; the temp file is per process.
- ffmpeg encodes and the Playwright availability probe have timeouts
  (`$DARKROOM_FFMPEG_TIMEOUT`, default 900 s; 60 s).
- Xvfb's display pipe is closed if Xvfb fails to start.
- `darkroom shoot` exits `130` on Ctrl-C and `143` on SIGTERM, after cleaning up, instead of
  a traceback.

### Docs

- README: "Stopping a run" (`ps`, `stop`, and why never `pkill -f`), `lockTimeout`, parallel
  runs of one manifest, a "Used by" gallery (Tangle and Frozen Star), and the demo images
  reshot with 0.4.

## 0.4.0 (2026-11-15)

0.3 manifests keep working unchanged, and the hashes of unchanged shots are the same, so
nothing is reshot on upgrade.

### Added

- **Prep shots** (#10): `"kind": "prep"` runs a command that writes files rather than
  images (a server and bots for several minutes, a simulation), under its `lock`. It declares
  the files it writes in `outputs` and is skipped while its argv, env, vars and inputs are
  unchanged and those files exist. Its default `cwd` is the manifest's folder.
- **`inputs`** on any shot (#10): paths, globs or directories whose file contents feed the
  capture hash, so a changed input reshoots the shot instead of leaving it "up to date".
- **`depends`** (#10): shot names that run first. Under `--only`, the preps a shot depends on
  run too (unless up to date); a prep's outputs count as the dependent shot's inputs; when a
  shot fails, the shots that depend on it are skipped. Cycles are a validation error.
- **Named outputs on command stills** (#11): `"outputs": {"top": "top.png", "oblique":
  "oblique.png"}` turns files one run writes into `{out_dir}` into `<name>-top.jpg`,
  `<name>-oblique.jpg` (or a `file` template with `{key}`). Each entry can carry its own
  `caption` and `alt` for the gallery.
- **`darkroom check` warnings** (#12): a command shot's program that is not on `PATH` or at
  its path, relative path-looking `argv` entries that do not exist, `inputs` that match
  nothing and missing `initScript` files are reported as warnings; the exit code is unchanged.
- **Web `initScript`** (#6): JS (a string, `{"file": path}`, or a list) that runs before the
  page's own scripts via `context.addInitScript`, to seed storage or wrap
  `requestAnimationFrame`. A file's contents feed the capture hash.
- **Web `context`** (#6): Playwright `browser.newContext` options passed through
  (`locale`, `timezoneId`, `colorScheme`, `reducedMotion`, `geolocation`, `permissions`,
  ...). Options darkroom sets from `device` or `userAgent` are rejected with a pointer there.

### Fixed

- `gallery`: a clip with video but no GIF shows its poster linked to the video again (#9).

## 0.3.0 (2026-11-15)

From a second pilot: a Strata web-kit game shot in software WebGL on a heavily loaded machine.
0.2 manifests keep working unchanged.

### Changed

- **GIFs are much smaller again, and still decode everywhere.** darkroom now writes GIFs with its
  own inter-frame optimization: each frame after the first marks pixels unchanged from the
  frame before with a reserved transparent palette index and is cropped to the box around
  its changes (or stored as a plain crop when that is smaller), with disposal 1 and a complete
  colour table (one global 256-entry table for `adaptive`, a full table per frame for
  `octree`/`median`). Identical frames merge into one longer frame. 0.2 had dropped Pillow's
  optimizer (it wrote indices past trimmed palettes, which ImageMagick rejects); on a mostly
  static 47-frame clip the GIF goes from 406 KB to 120 KB, and no clip gets bigger.
- **Resume survives upgrades and harmless edits.** The single shot hash is split into a
  *capture* hash (url or argv, steps, device, adapter and the other fields that change the
  pixels, plus the vars) and an *encode* hash (formats, budgets, palettes, sizes, `file`, ...).
  A changed capture hash reshoots; a changed encode hash only re-encodes the saved take.
  Neither includes the darkroom version, `retries`, `lock`, `timeout`, `comment`, `caption`,
  `alt` or gallery options, so none of those mark a shot stale any more (#2). State written by
  0.1/0.2 is recognised and adopted when the shot has not changed.
- **`waitFor` steps wait.** They returned at once whatever the expression said, because
  Playwright treated the async predicate's Promise as truthy (#4). They now poll in the page
  and fail the attempt on timeout.
- **One timeout for every Playwright call.** Screenshots, clicks, taps and navigation use
  `screenshotTimeout` (ms), else the shot's `timeout`, else 120 s, instead of Playwright's
  30 s default that every attempt missed on a loaded machine (#5).
- `gallery --insert` ignores markers inside inline code and fenced code blocks (#1).
- `--only` can be repeated: `--only a --only b` shoots both (#8).

### Added

- Steps: `{"until": "expr", "steps": [...], "max": N, "then": M}` repeats steps until the
  expression holds (at most `max` passes), then runs `then` more passes (#7);
  `{"goto": "url"}` and `{"reload": true}`, each followed by the ready wait and the shot's
  `css`; `{"tap": "css selector"}` taps an element's centre (touchscreen on touch devices);
  `{"frames": 3, "step": 0.1}`.
- `frameStep`: `strata-kit` seconds per driven frame (default 1/30).
- Named stills: `{"capture": "name"}` in a still writes `name.jpg` per capture, so one browser
  session makes several stills without replaying.
- `gif.hold_last_ms` and `video: {"formats": [...], "hold_last": seconds}` hold the last frame
  (#7).
- `every`: keep every Nth captured frame of a clip; `gif.every` and `video.every` override it.
- `colors` on PNG stills, pairs and grids: quantize to at most N colours.
- `gallery: {"width": 480, "poster": true}` per shot: `<img width=...>` in the gallery, and a
  clip's poster beside its GIF (#3).
- `darkroom shoot --out DIR` writes every output (including `file` ones) into a scratch dir
  for review, with its own state record; the takes are shared.
- Each take's `meta.json` and each state entry record the darkroom version that made it. The
  README shows how to pin a version on shared machines.

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
