"""Run-scoped process control: every darkroom run owns its processes, and only those.

Each child darkroom starts (the Node runner and the Chromium it launches, command programs,
Xvfb, ffmpeg) runs in a new session, so its process group is its own and a signal sent to
the darkroom run never needs a pattern match. A run registers itself in the run directory
(``$DARKROOM_RUN_DIR``, else ``$XDG_RUNTIME_DIR/darkroom-runs``, else
``/tmp/darkroom-runs-<uid>``) as ``<run-id>.json``, with its pid, the process groups it is
running, the manifest, cwd, start time and current shot. ``darkroom ps`` lists the live runs
and ``darkroom stop`` stops one of them; nothing else is touched.

On exit, error, Ctrl-C (SIGINT), SIGTERM or SIGHUP a run kills its own groups (and any
descendant that started its own session, such as Chromium under Playwright) before it exits.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

POSIX = os.name == "posix"
GRACE = 3.0  # seconds between SIGTERM and SIGKILL


class Stopped(BaseException):
    """Raised in the main thread on SIGTERM/SIGHUP. A BaseException (like KeyboardInterrupt), so
    the per-shot ``except Exception`` does not swallow it and the run stops."""


def run_dir() -> Path:
    if os.environ.get("DARKROOM_RUN_DIR"):
        return Path(os.environ["DARKROOM_RUN_DIR"])
    if os.environ.get("XDG_RUNTIME_DIR"):
        return Path(os.environ["XDG_RUNTIME_DIR"]) / "darkroom-runs"
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(f"/tmp/darkroom-runs-{uid}")


# ------------------------------------------------------------------ /proc helpers

def _stat(pid: int) -> list[str] | None:
    """Fields of /proc/PID/stat after the command name (so index 0 is the state)."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return raw[raw.rfind(")") + 2:].split()


def proc_start(pid: int) -> str | None:
    """The process's start time (clock ticks since boot): with the pid, an identity that a
    recycled pid does not share. None where there is no /proc."""
    st = _stat(pid)
    return st[19] if st and len(st) > 19 else None


def alive(pid: int | None, start: str | None = None) -> bool:
    """Is PID running (not a zombie) and, if ``start`` is given, still the same process?"""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    st = _stat(pid)
    if st is None:
        return not Path("/proc").is_dir()  # no /proc: trust kill(0)
    if st[0] in ("Z", "X"):
        return False
    return start is None or st[19] == start


def _table() -> dict[int, tuple[int, int, str]]:
    """pid -> (ppid, pgid, start) for every process we can see."""
    out = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        st = _stat(int(n))
        if st and len(st) > 19 and st[0] not in ("Z", "X"):
            out[int(n)] = (int(st[1]), int(st[2]), st[19])
    return out


def descendants(pid: int, table=None) -> list[int]:
    table = _table() if table is None else table
    kids: dict[int, list[int]] = {}
    for p, (pp, _, _) in table.items():
        kids.setdefault(pp, []).append(p)
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def group_members(pgid: int, table=None) -> list[int]:
    table = _table() if table is None else table
    return [p for p, (_, g, _) in table.items() if g == pgid]


def _signal(pids, pgids, sig) -> None:
    for g in pgids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(g, sig)
    for p in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(p, sig)


def kill_tree(pid: int, grace: float = GRACE, also_group: bool = True) -> None:
    """SIGTERM PID's process group and every descendant (and their groups: Chromium starts its
    own), wait up to ``grace`` seconds, then SIGKILL whatever is left. Only processes that
    descend from PID, or share its group, are signalled."""
    if not POSIX:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        return
    table = _table()
    pids = set(descendants(pid, table))
    if alive(pid):
        pids.add(pid)
    pgids = {table[p][1] for p in pids if p in table}
    if also_group:
        pgids.add(pid)  # a child started with start_new_session leads its own group
    pgids.discard(os.getpgid(0))  # never our own group
    _signal(pids, pgids, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        table = _table()
        left = [p for p in pids if p in table] + [p for g in pgids for p in group_members(g, table)]
        if not left:
            return
        time.sleep(0.05)
    table = _table()
    _signal([p for p in pids if p in table], pgids, signal.SIGKILL)


# ------------------------------------------------------------------ the run registry

class Run:
    """This process's entry in the run registry."""

    def __init__(self, manifest: str | None, argv: list[str] | None = None):
        self.id = secrets.token_hex(3)
        self.path = run_dir() / f"{self.id}.json"
        self.data = {
            "id": self.id, "pid": os.getpid(), "pgid": os.getpgid(0) if POSIX else None,
            "start": proc_start(os.getpid()), "manifest": manifest, "cwd": os.getcwd(),
            "started": _dt.datetime.now().isoformat(timespec="seconds"), "argv": argv or sys.argv,
            "shot": None, "groups": {},
        }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.path.with_name(f".{self.id}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)
        except OSError:
            pass  # the registry is a convenience; never fail a shoot over it

    def set_shot(self, name: str | None) -> None:
        self.data["shot"] = name
        self.save()

    def add_group(self, pid: int, argv) -> None:
        self.data["groups"][str(pid)] = {"start": proc_start(pid), "argv": [str(a) for a in argv][:6]}
        self.save()

    def drop_group(self, pid: int) -> None:
        if self.data["groups"].pop(str(pid), None) is not None:
            self.save()

    def kill_all(self, grace: float = GRACE) -> None:
        for pid in list(self.data["groups"]):
            kill_tree(int(pid), grace)
        self.data["groups"] = {}

    def end(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()


_current: Run | None = None
_stopping = False


def current() -> Run | None:
    return _current


def _on_signal(signum, frame):
    global _stopping
    if _stopping:
        return  # cleanup is under way and bounded; a second signal changes nothing
    _stopping = True
    raise Stopped(signal.Signals(signum).name)


@contextlib.contextmanager
def run_scope(manifest: str | None):
    """Register this run, turn SIGTERM/SIGHUP into ``Stopped``, and on the way out (normal,
    error, Ctrl-C or signal) kill every process group the run still owns."""
    global _current, _stopping
    if _current is not None:  # nested (e.g. tests calling the CLI twice): reuse
        yield _current
        return
    run = Run(str(Path(manifest).resolve()) if manifest else None)
    _current, _stopping = run, False
    old = {}
    if POSIX and _main_thread():
        for s in (signal.SIGTERM, signal.SIGHUP):
            old[s] = signal.signal(s, _on_signal)
    run.save()
    try:
        yield run
    finally:
        try:
            run.kill_all()
        finally:
            run.end()
            for s, h in old.items():
                signal.signal(s, h)
            _current = None


def _main_thread() -> bool:
    import threading
    return threading.current_thread() is threading.main_thread()


# ------------------------------------------------------------------ spawning

def spawn(argv, **kw) -> subprocess.Popen:
    """Popen in a new session (its own process group), registered with the current run."""
    if POSIX:
        kw.setdefault("start_new_session", True)
    p = subprocess.Popen(argv, **kw)
    if _current is not None:
        _current.add_group(p.pid, argv)
    return p


def reap(p: subprocess.Popen, grace: float = GRACE) -> None:
    """Kill P's group and descendants (if any are left) and forget it."""
    try:
        if POSIX:
            table = _table()
            if p.poll() is None or group_members(p.pid, table) or descendants(p.pid, table):
                kill_tree(p.pid, grace)
        elif p.poll() is None:
            p.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(grace)
    finally:
        if _current is not None:
            _current.drop_group(p.pid)


def run(argv, *, timeout: float | None = None, input=None, text: bool = False, env=None, cwd=None,
        check: bool = False) -> subprocess.CompletedProcess:
    """Like ``subprocess.run(capture_output=True)``, but the child gets its own process group,
    and on timeout, error or interrupt the whole tree is killed. A timeout no longer hangs on a
    grandchild that still holds the output pipes: the drain after the kill is bounded too.
    Leftover group members (a daemon the program forked and forgot) are killed on exit."""
    p = spawn(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, env=env, cwd=cwd)
    try:
        try:
            out, err = p.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(p.pid)
            try:
                out, err = p.communicate(timeout=GRACE)
            except subprocess.TimeoutExpired:
                for f in (p.stdout, p.stderr):
                    with contextlib.suppress(OSError):
                        f.close()
                p.wait(GRACE)
                out, err = ("" if text else b""), ("" if text else b"")
            raise subprocess.TimeoutExpired(argv, timeout, out, err)
    except BaseException:
        reap(p)
        raise
    reap(p)
    cp = subprocess.CompletedProcess(argv, p.returncode, out, err)
    if check:
        cp.check_returncode()
    return cp


# ------------------------------------------------------------------ ps / stop

def _group_live(pgid: int, start: str | None, table) -> bool:
    """Does the group still hold processes of ours? Its leader with the same start time, or
    (the leader gone) members that started no earlier than it did."""
    lead = table.get(pgid)
    if lead is not None:
        return start is None or lead[2] == start
    members = group_members(pgid, table)
    if not members:
        return False
    return start is None or any(int(table[m][2]) >= int(start) for m in members)


def runs(prune: bool = True) -> list[dict]:
    """Registered runs, oldest first. Each has ``alive`` and ``live_groups``. A run whose
    darkroom process is gone and that left nothing running is pruned (deleted)."""
    d = run_dir()
    out = []
    table = _table()
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            r = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        r["path"] = str(f)
        r["alive"] = alive(r.get("pid"), r.get("start"))
        r["live_groups"] = [int(g) for g, info in (r.get("groups") or {}).items()
                            if _group_live(int(g), info.get("start"), table)]
        if not r["alive"] and not r["live_groups"]:
            if prune:
                with contextlib.suppress(OSError):
                    f.unlink()
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("started") or "")
    return out


def find(run_id: str | None = None, manifest: str | None = None) -> list[dict]:
    rs = runs()
    if run_id:
        return [r for r in rs if r["id"] == run_id] or [r for r in rs if r["id"].startswith(run_id)]
    if manifest:
        want = str(Path(manifest).resolve())
        return [r for r in rs if r.get("manifest") == want]
    return []


def stop(r: dict, timeout: float = 15.0) -> list[str]:
    """Stop one run: SIGTERM its darkroom process (which kills its own groups on the way out),
    wait up to ``timeout``, then kill whatever of its groups is left and the process itself.
    Returns what was done, for the user."""
    done = []
    pid, start = r.get("pid"), r.get("start")
    if alive(pid, start):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        done.append(f"sent SIGTERM to darkroom (pid {pid})")
        deadline = time.monotonic() + timeout
        while alive(pid, start) and time.monotonic() < deadline:
            time.sleep(0.1)
    table = _table()
    for g, info in (r.get("groups") or {}).items():
        if _group_live(int(g), info.get("start"), table):
            kill_tree(int(g))
            done.append(f"killed process group {g} ({' '.join(info.get('argv') or [])[:60]})")
    if alive(pid, start):
        for c in descendants(pid):
            kill_tree(c)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        done.append(f"killed darkroom (pid {pid}): it did not exit within {timeout:g}s")
    with contextlib.suppress(OSError):
        Path(r["path"]).unlink()
    return done
