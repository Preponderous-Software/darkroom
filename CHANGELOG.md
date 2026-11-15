# Changelog

## 0.1.0 (unreleased)

First version.

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
