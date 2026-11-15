# Contributing

Issues and pull requests are welcome.

```sh
uv venv && uv pip install -e '.[dev]'      # or: python -m venv .venv && pip install -e '.[dev]'
pytest -m "not browser"                     # fast: manifests, encoders, command backend, gallery
NODE_PATH=/path/to/node_modules pytest -m browser   # one Playwright test (needs Node + Chromium)
```

- Keep the core on Pillow alone; anything heavier is an optional extra.
- A new step, backend or option needs a validator rule with a clear message, a test, and a
  line in the README's manifest reference.
- Keep sample images in `docs/` small (a still under ~150 KB, a GIF under ~1 MB).
