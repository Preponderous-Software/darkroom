import json
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def write_manifest(tmp_path):
    def write(data, name="media.json"):
        p = tmp_path / name
        p.write_text(json.dumps(data))
        return p
    return write


@pytest.fixture
def fake_cmd():
    return [sys.executable, str(FIXTURES / "fake_capture.py")]


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    monkeypatch.setenv("DARKROOM_LOCK_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def run_dir(tmp_path, monkeypatch):
    """Keep every test's run registry out of the real one (other runs may be live)."""
    d = tmp_path / "runs"
    monkeypatch.setenv("DARKROOM_RUN_DIR", str(d))
    return d
