"""Run-scoped process control: each run owns its process groups, and only those."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from darkroom import procs
from darkroom.cli import main
from darkroom.manifest import load

pytestmark = pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc (Linux)")

FIXTURES = Path(__file__).parent / "fixtures"


def wait_for(cond, timeout=60.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def pids_in(d: Path) -> dict:
    return {f.stem: int(f.read_text()) for f in d.glob("*.pid") if f.read_text().strip()}


def hanging_manifest(tmp_path: Path, name: str) -> tuple[Path, Path]:
    pid_dir = tmp_path / f"{name}-pids"
    proj = tmp_path / name
    proj.mkdir()
    p = proj / "media.json"
    p.write_text(json.dumps({"shots": [{
        "name": "hang", "kind": "still", "backend": "command", "retries": 0, "timeout": 300,
        "device": {"width": 32, "height": 32},
        "argv": [sys.executable, str(FIXTURES / "sleeper.py"), str(pid_dir)]}]}))
    return p, pid_dir


def start_shoot(manifest: Path, **env) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "darkroom", "shoot", str(manifest)], env={**os.environ, **env},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def all_dead(pids) -> bool:
    return not any(procs.alive(p) for p in pids)


def cleanup(*popens_and_pids):
    for x in popens_and_pids:
        pid = x.pid if isinstance(x, subprocess.Popen) else x
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_two_runs_stop_independently(tmp_path, capsys):
    a, a_pids = hanging_manifest(tmp_path, "a")
    b, b_pids = hanging_manifest(tmp_path, "b")
    pa, pb = start_shoot(a), start_shoot(b)
    try:
        wait_for(lambda: len(pids_in(a_pids)) == 3 and len(pids_in(b_pids)) == 3, what="both captures")
        runs = procs.runs()
        assert sorted(r["manifest"] for r in runs) == sorted([str(a.resolve()), str(b.resolve())])
        assert all(r["shot"] == "hang" and r["alive"] for r in runs)

        assert main(["ps"]) == 0
        out = capsys.readouterr().out
        assert "hang" in out and str(a.resolve()) in out

        assert main(["stop", "--manifest", str(a)]) == 0
        assert "stopped" in capsys.readouterr().out
        pa.wait(30)
        wait_for(lambda: all_dead(pids_in(a_pids).values()), 15, "run a's processes to exit")
        # Run b, its program and the program's children are untouched.
        assert pb.poll() is None
        assert not any(not procs.alive(p) for p in pids_in(b_pids).values())
        assert [r["manifest"] for r in procs.runs()] == [str(b.resolve())]

        b_id = procs.runs()[0]["id"]
        assert main(["stop", b_id]) == 0
        pb.wait(30)
        wait_for(lambda: all_dead(pids_in(b_pids).values()), 15, "run b's processes to exit")
        assert procs.runs() == []
        assert pa.returncode == 143 and pb.returncode == 143
    finally:
        cleanup(pa, pb, *pids_in(a_pids).values(), *pids_in(b_pids).values())


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_leaves_no_orphans(tmp_path, run_dir, sig):
    m, pid_dir = hanging_manifest(tmp_path, "m")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    p = start_shoot(m, TMPDIR=str(tmp))
    try:
        wait_for(lambda: len(pids_in(pid_dir)) == 3, what="the capture")
        os.kill(p.pid, sig)
        p.wait(30)
        out = p.stdout.read()
        assert p.returncode == (143 if sig == signal.SIGTERM else 130), out
        # The program, its child and the child that started its own session are all gone.
        wait_for(lambda: all_dead(pids_in(pid_dir).values()), 15, "the capture's processes to exit")
        assert list(run_dir.glob("*.json")) == []
        assert list(tmp.iterdir()) == []  # the run's scratch dirs were removed on the way out
    finally:
        cleanup(p, *pids_in(pid_dir).values())


def test_timeout_kills_the_tree_and_does_not_hang_on_held_pipes(tmp_path):
    # The program's child keeps stdout open: subprocess.run would wait for it forever.
    pid_dir = tmp_path / "pids"
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        procs.run([sys.executable, str(FIXTURES / "sleeper.py"), str(pid_dir)], timeout=3)
    assert time.monotonic() - t0 < 20
    pids = pids_in(pid_dir)
    assert pids and all_dead(pids.values())


def test_run_kills_leftover_group_members(tmp_path):
    # A program that forks a helper and exits: the helper is not left running.
    pid_file = tmp_path / "helper.pid"
    code = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))")
    r = procs.run([sys.executable, "-c", code], timeout=30)
    assert r.returncode == 0
    helper = int(pid_file.read_text())
    try:
        wait_for(lambda: not procs.alive(helper), 10, "the helper to be killed")
    finally:
        cleanup(helper)


def test_ps_prunes_stale_entries(run_dir, capsys):
    run_dir.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (run_dir / "abc123.json").write_text(json.dumps({
        "id": "abc123", "pid": dead.pid, "start": "1", "manifest": "/nowhere/media.json",
        "started": "2026-01-01T00:00:00", "shot": "x", "groups": {}}))
    (run_dir / "junk.json").write_text("{not json")
    assert procs.runs() == []
    assert not (run_dir / "abc123.json").exists()
    assert main(["ps"]) == 0
    assert "no darkroom runs" in capsys.readouterr().out
    assert main(["stop", "abc123"]) == 1


def test_stop_needs_exactly_one_target(capsys):
    assert main(["stop"]) == 2
    assert main(["stop", "x", "--manifest", "media.json"]) == 2


def test_in_process_shoot_registers_and_cleans_up(write_manifest, fake_cmd, run_dir):
    seen = []
    orig = procs.Run.add_shot

    def spy(self, name):
        seen.append((name, json.loads(self.path.read_text())["pid"]))
        orig(self, name)

    p = write_manifest({"shots": [{"name": "t", "kind": "still", "backend": "command",
                                   "device": {"width": 32, "height": 32},
                                   "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]}]})
    procs.Run.add_shot = spy
    try:
        assert main(["shoot", str(p)]) == 0
    finally:
        procs.Run.add_shot = orig
    assert seen == [("t", os.getpid())]
    assert list(run_dir.glob("*.json")) == []
    assert procs.current() is None


def test_lock_timeout(write_manifest, fake_cmd):
    from darkroom.locks import lock
    from darkroom.shoot import shoot
    p = write_manifest({"shots": [{"name": "t", "kind": "still", "backend": "command", "retries": 0,
                                   "lock": "busy", "lockTimeout": 0.5, "device": {"width": 32, "height": 32},
                                   "argv": fake_cmd + ["{out_dir}", "{width}", "{height}"]}]})
    logs = []
    with lock("busy"):
        t0 = time.monotonic()
        assert shoot(load(p), log=logs.append) == 1
        assert time.monotonic() - t0 < 10
    text = "\n".join(logs)
    assert f"held by pid {os.getpid()}" in text and "still busy" in text


def test_lock_timeout_validated(write_manifest):
    from darkroom.manifest import ManifestError
    p = write_manifest({"shots": [{"name": "t", "kind": "still", "backend": "command", "lock": "x",
                                   "lockTimeout": -1, "argv": ["true"]}]})
    with pytest.raises(ManifestError, match="lockTimeout"):
        load(p)


def test_concurrent_shoots_of_one_manifest_keep_both_state_entries(write_manifest, fake_cmd):
    # --only a and --only b at once: each run's state update must not drop the other's entry.
    shots = [{"name": n, "kind": "still", "backend": "command", "device": {"width": 32, "height": 32},
              "argv": fake_cmd + ["{out_dir}", "{width}", "{height}", "--sleep", "{nap}"]}
             for n in ("a", "b", "c", "d")]
    p = write_manifest({"vars": {"nap": "0.5"}, "shots": shots})
    runs = [subprocess.Popen([sys.executable, "-m", "darkroom", "shoot", str(p), "--only", *names],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for names in (["a", "c"], ["b", "d"])]
    try:
        for r in runs:
            assert r.wait(240) == 0, r.stdout.read()
    finally:
        cleanup(*runs)
    state = json.loads((p.parent / ".darkroom" / "state.json").read_text())
    assert {"a", "b", "c", "d"} <= set(state)
    assert not list((p.parent / ".darkroom").glob("*.tmp"))
