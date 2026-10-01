"""A first folder import on a fresh data dir is visible without a restart.

Issue #3965. An engine booted on an EMPTY data dir used to bind an
InMemoryBackend for the life of the process: ``make_backend()`` picks by
whether ``state.db`` exists, and on a first run nothing had created it yet.
The import then wrote its rows to sqlite, readiness (which reads sqlite
directly) counted them, and ``/api/v1/tracks`` plus ``/health`` kept serving
the empty in-memory library until the engine was relaunched. The packaged
desktop shell creates only the data dir before spawning the engine, so every
new user's first import hit this.

The same run also carries the #3964 HTTP acceptance: /health and
/library/readiness are probed throughout the import and each must answer
within ROUTE_BOUND_S. On a fast local disk that half cannot go red (a write
costs ~0.1 ms here); test_jobs_progress_coalescing.py is the discriminating
test for #3964, and the agentbox 10k measurement is the real-disk proof.

Runs in a SUBPROCESS, like test_migrate_on_boot.py: ``create_app`` imports
the legacy modules, which resolve their paths from ``MDT_DATA_DIR`` at import
time. The import is the product's own ``POST /api/v1/setup/import/folder``
driving the real worker subprocess over real, playable wav files.

Single-line intent:
  - [if] a fresh engine runs a first import [then] /tracks lists it unrestarted, [else stop]
  - if an engine boots with no state.db and a folder import finishes then
    /api/v1/tracks lists the imported rows without a restart [broken if the
    backend is chosen by file presence before the store exists, per #3965]
  - if /api/v1/health is read after that import then state_db.tracks equals
    the readiness total [broken if the two surfaces read different stores]
  - if health and readiness are probed during the import then every probe
    answers 200 within ROUTE_BOUND_S
  - if the engine reboots on that now-populated data dir then it serves the
    same rows [broken if boot-time store creation clobbers an existing db]
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.requirement("SETUP-20")

REPO_ROOT = Path(__file__).resolve().parents[2]
FILES = 40
ROUTE_BOUND_S = 1.0

_PROBE = """
import json
import os
import time
from pathlib import Path

from starlette.testclient import TestClient

from apps.engine_core.app import HEALTH_PATH, create_app
from apps.engine_core.config import EngineConfig

data_dir = Path(os.environ["MDT_DATA_DIR"])
library = os.environ["PROBE_LIBRARY"]
state_db = data_dir / "state" / "state.db"
app = create_app(EngineConfig(data_dir=data_dir))
out = {
    "state_db_at_compose": state_db.is_file(),
    "backend": type(app.state.backend).__name__,
    "probes": [],
}

def timed(client, path):
    started = time.monotonic()
    response = client.get(path)
    return response.status_code, time.monotonic() - started

# SEC-01 (#2689): bare subprocess, so base_url must be explicit or the
# daemon host allowlist 403s every request. Server errors come back as 500s
# so a failing probe is COUNTED below rather than ending the run.
with TestClient(
    app, base_url="http://127.0.0.1", raise_server_exceptions=False
) as client:
    queued = client.post(
        "/api/v1/setup/import/folder", json={"folders": [library]}
    )
    out["enqueue_status"] = queued.status_code
    out["enqueue_body"] = queued.json()
    job_id = queued.json()["id"]
    deadline = time.monotonic() + 120
    while True:
        for path in (HEALTH_PATH, "/api/v1/library/readiness?limit=1"):
            code, seconds = timed(client, path)
            out["probes"].append({"path": path, "code": code, "s": seconds})
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
            break
        if time.monotonic() > deadline:
            break
        time.sleep(0.05)
    out["job"] = job
    out["tracks"] = client.get("/api/v1/tracks?limit=500").json()
    out["health"] = client.get(HEALTH_PATH).json()
    out["readiness"] = client.get("/api/v1/library/readiness?limit=1").json()
    out["setup_status"] = client.get("/api/v1/setup/status").json()

print(json.dumps(out))
"""


_REBOOT_PROBE = """
import json
import os
from pathlib import Path

from starlette.testclient import TestClient

from apps.engine_core.app import HEALTH_PATH, create_app
from apps.engine_core.config import EngineConfig

app = create_app(EngineConfig(data_dir=Path(os.environ["MDT_DATA_DIR"])))
with TestClient(app, base_url="http://127.0.0.1") as client:
    out = {
        "backend": type(app.state.backend).__name__,
        "tracks": client.get("/api/v1/tracks?limit=500").json(),
        "health": client.get(HEALTH_PATH).json(),
    }
print(json.dumps(out))
"""


def _run_probe(script: str, data_dir: Path, **extra_env: str) -> dict[str, Any]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "MDT_DATA_DIR": str(data_dir),
            "MDT_LIBRARY_MODE": "local",
            **extra_env,
        }
    )
    env.pop("WEB_CONCURRENCY", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=240,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"engine probe failed (exit {result.returncode})\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-4000:]}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _probe_subdict(probe: dict[str, Any], key: str) -> dict[str, Any]:
    raw = probe.get(key)
    return raw if isinstance(raw, dict) else {}


def _first_import_failure_evidence(probe: dict[str, Any]) -> dict[str, Any]:
    """Compact first-import facts for zero-row or wrong-count assertion output."""
    tracks = _probe_subdict(probe, "tracks")
    items = tracks.get("items")
    item_count = len(items) if isinstance(items, list) else None
    job = _probe_subdict(probe, "job")
    readiness = _probe_subdict(probe, "readiness")
    setup = _probe_subdict(probe, "setup_status")
    last_import_raw = setup.get("last_import")
    last_import = last_import_raw if isinstance(last_import_raw, dict) else {}
    health = _probe_subdict(probe, "health")
    state_db_raw = health.get("state_db")
    state_db = state_db_raw if isinstance(state_db_raw, dict) else {}
    return {
        "tracks_items": item_count,
        "expected_files": FILES,
        "job_status": job.get("status"),
        "job_message": job.get("message"),
        "job_error": job.get("error"),
        "readiness_total_tracks": readiness.get("total_tracks"),
        "readiness_status": readiness.get("status"),
        "readiness_detail": readiness.get("detail"),
        "setup_tracks": setup.get("tracks"),
        "last_import": {
            k: last_import.get(k)
            for k in (
                "files_seen",
                "files_rejected_unplayable",
                "tracks",
                "tracks_written",
                "status",
                "error",
            )
        },
        "health_state_db_tracks": state_db.get("tracks"),
        "backend": probe.get("backend"),
        "state_db_at_compose": probe.get("state_db_at_compose"),
    }


def _write_wav(path: Path, seconds: float = 0.1) -> None:
    """A real, playable wav: the worker's playability gate reads it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(44100 * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    data_dir = tmp_path_factory.mktemp("engine-empty-data")
    library = tmp_path_factory.mktemp("first-import-library")
    for index in range(FILES):
        _write_wav(library / f"crate-{index % 4}" / f"track-{index:03d}.wav")
    assert not (data_dir / "state" / "state.db").exists()
    out = _run_probe(_PROBE, data_dir, PROBE_LIBRARY=str(library))
    out["data_dir"] = str(data_dir)
    return out


@pytest.fixture(scope="module")
def reboot(probe: dict[str, Any]) -> dict[str, Any]:
    """A second engine on the data dir the first import populated."""
    return _run_probe(_REBOOT_PROBE, Path(probe["data_dir"]))


def test_the_import_itself_succeeded(probe: dict[str, Any]) -> None:
    assert probe["enqueue_status"] == 202, probe["enqueue_body"]
    assert probe["job"]["status"] == "succeeded", probe["job"]


def test_tracks_lists_the_first_import_without_a_restart(
    probe: dict[str, Any],
) -> None:
    items = probe["tracks"]["items"]
    assert len(items) == FILES, (
        f"/api/v1/tracks listed {len(items)} of {FILES} imported rows on the "
        f"engine that ran the import (backend={probe['backend']}); "
        f"first_import={_first_import_failure_evidence(probe)!r}"
    )


def test_health_counts_agree_with_readiness(probe: dict[str, Any]) -> None:
    health_tracks = probe["health"]["state_db"]["tracks"]
    readiness_total = probe["readiness"]["total_tracks"]
    assert readiness_total == FILES, (
        f"readiness total_tracks={readiness_total}, expected {FILES}; "
        f"first_import={_first_import_failure_evidence(probe)!r}"
    )
    assert health_tracks == readiness_total, (
        f"/health state_db.tracks={health_tracks} but readiness "
        f"total_tracks={readiness_total}"
    )


def test_the_engine_serves_its_store_from_boot(probe: dict[str, Any]) -> None:
    """The root cause, read directly: the store exists before the backend is
    chosen, so the choice cannot fall to an in-memory library."""
    assert probe["state_db_at_compose"] is True, probe
    assert probe["backend"] == "SqliteBackend", probe


def test_health_and_readiness_answer_throughout_the_import(
    probe: dict[str, Any],
) -> None:
    probes = probe["probes"]
    assert len(probes) >= 2, probes
    failed = [p for p in probes if p["code"] != 200]
    assert not failed, f"{len(failed)} probes did not answer 200: {failed[:4]}"
    slowest = max(p["s"] for p in probes)
    assert slowest < ROUTE_BOUND_S, (
        f"a probe took {slowest:.3f}s during the import "
        f"(bound {ROUTE_BOUND_S}s)"
    )


def test_a_reboot_on_the_populated_dir_serves_the_same_rows(
    probe: dict[str, Any], reboot: dict[str, Any]
) -> None:
    """Control: creating the store at boot must leave an existing one alone."""
    assert reboot["backend"] == "SqliteBackend", reboot
    first = sorted(item["stable_id"] for item in probe["tracks"]["items"])
    again = sorted(item["stable_id"] for item in reboot["tracks"]["items"])
    assert again == first, (
        f"{len(again)} rows after reboot, {len(first)} before; "
        f"first_import={_first_import_failure_evidence(probe)!r}"
    )
    assert reboot["health"]["state_db"]["tracks"] == FILES, (
        f"reboot /health state_db.tracks={reboot['health']['state_db']['tracks']}, "
        f"expected {FILES}; reboot_health={reboot['health']!r}; "
        f"first_import={_first_import_failure_evidence(probe)!r}"
    )
