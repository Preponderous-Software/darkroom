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
