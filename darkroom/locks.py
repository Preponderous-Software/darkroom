"""Named, machine-wide file locks: one heavy capture at a time.

``with lock("pt"):`` takes an exclusive ``fcntl.flock`` on ``<lock dir>/pt.lock``. The lock
dir is ``$DARKROOM_LOCK_DIR`` or ``/tmp/darkroom-locks``. Locks are released when the
process exits, even on a crash, so there is nothing stale to clean up.
"""
from __future__ import annotations

import contextlib
import os
import re
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: locks become no-ops
    fcntl = None  # type: ignore


def lock_dir() -> Path:
    return Path(os.environ.get("DARKROOM_LOCK_DIR", "/tmp/darkroom-locks"))


class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def lock(name: str | None, timeout: float | None = None, on_wait=None):
    """Hold the named lock for the duration of the block. ``name=None`` is a no-op.
    ``on_wait(path)`` is called once if the lock is busy."""
    if not name or fcntl is None:
        yield None
        return
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError(f"lock name '{name}': letters, digits, '.', '_' and '-' only")
    d = lock_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        start = time.monotonic()
        warned = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not warned and on_wait:
                    on_wait(path)
                    warned = True
                if timeout is not None and time.monotonic() - start > timeout:
                    raise LockTimeout(f"lock '{name}' still busy after {timeout:g}s ({path})")
                time.sleep(0.25)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield path
    finally:
        os.close(fd)  # closing releases the flock
