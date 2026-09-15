"""An engine started with --data-dir must write everything inside it.

Found during an e2e run on an isolated --data-dir: the engine still wrote
browser-error records to ``~/.local/share/music-dj-tools/webui/``, so the
sandbox leaked one process-global path and two parallel lanes shared a log.

Run in a SUBPROCESS for the same reason as test_contract_rev: create_app
imports the legacy modules, which resolve their paths from MDT_DATA_DIR at
import time. The child also gets its own HOME, so "did it write to the home
path" is answered by a directory this test owns rather than by the state of
the developer's machine.

Single-line intent:
  - if a --data-dir engine writes client diagnostics outside that data dir
    then the sandbox is not a sandbox and parallel lanes corrupt each other
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.engine_core.config import EngineBootError, EngineConfig, prepare_layout

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import json
import os
from pathlib import Path

from starlette.testclient import TestClient

from apps.engine_core.app import create_app
from apps.engine_core.config import EngineConfig

data_dir = Path(os.environ["MDT_DATA_DIR"])
app = create_app(EngineConfig(data_dir=data_dir))

payload = {
    "client_event_id": "sandbox-probe-1",
    "kind": "window-error",
    "message": "data-dir sandbox probe",
    "url": "http://127.0.0.1:8585/",
    "client_timestamp": "2026-08-19T12:00:00.000Z",
    "user_agent": "probe",
    "secure_context": True,
    "audio_worklet_available": False,
}
# SEC-01 (#2689): this runs in a bare subprocess, so it never imports
# tests/conftest.py's TestClient default -- base_url must be explicit here
# or the daemon host allowlist 403s every request.
with TestClient(app, base_url="http://127.0.0.1") as client:
    errors = client.post("/api/v1/client-errors", json=payload)
    events = client.post("/api/v1/client-events", json={
        "client_event_id": "sandbox-probe-2",
        "kind": "page-view",
        "url": "http://127.0.0.1:8585/performance",
        "path": "/performance",
        "referrer": None,
        "client_timestamp": "2026-08-19T12:00:00.000Z",
        "user_agent": "probe",
        "language": "en-GB",
        "secure_context": True,
        "viewport_width": 1280,
        "viewport_height": 800,
    })

print(json.dumps({
    "errors_status": errors.status_code,
    "errors_stored": errors.json().get("stored"),
    "events_status": events.status_code,
    "in_data_dir": sorted(str(p.relative_to(data_dir)) for p in data_dir.rglob("*.log")),
    "in_home": sorted(str(p) for p in Path(os.environ["HOME"]).rglob("*.log")),
}))
"""


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict:
    data_dir = tmp_path_factory.mktemp("engine-data")
    home_dir = tmp_path_factory.mktemp("engine-home")
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "MDT_DATA_DIR": str(data_dir),
            "MDT_LIBRARY_MODE": "local",
            "HOME": str(home_dir),
        }
    )
    env.pop("WEB_CONCURRENCY", None)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"engine sandbox probe failed (exit {result.returncode})\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-4000:]}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_client_errors_are_written_inside_the_data_dir(probe: dict) -> None:
    assert probe["errors_status"] == 202
    assert probe["errors_stored"] is True
    assert any(
        "client-errors" in name for name in probe["in_data_dir"]
    ), f"no client-errors log under the data dir; found {probe['in_data_dir']}"


def test_client_events_are_written_inside_the_data_dir(probe: dict) -> None:
    assert probe["events_status"] == 202
    assert any(
        "visitors" in name for name in probe["in_data_dir"]
    ), f"no visitor log under the data dir; found {probe['in_data_dir']}"


def test_nothing_is_written_to_the_home_directory(probe: dict) -> None:
    assert probe["in_home"] == [], (
        "a --data-dir engine wrote outside its data dir: "
        f"{probe['in_home']}"
    )


def test_boot_refuses_an_unwritable_logs_dir(tmp_path: Path) -> None:
    """No silent fallback to the home path: an engine that cannot write its
    own diagnostics stops at boot and says which dir and why."""
    data_dir = tmp_path / "engine-data"
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True)
    logs_dir.chmod(0o500)
    try:
        with pytest.raises(EngineBootError) as excinfo:
            prepare_layout(EngineConfig(data_dir=data_dir))
    finally:
        logs_dir.chmod(0o700)
    assert str(logs_dir) in str(excinfo.value)
    assert "not writable" in str(excinfo.value)
