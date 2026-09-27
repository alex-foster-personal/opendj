"""The packaged engine arms the user-jobs drain and requests R2 stem hydration.

The installed app builds its FastAPI app through
``apps.engine_core.app.create_app``, not the standalone webui daemon entry
point, so a flag armed only in the daemon entry point is silently OFF in the
shipped app. On Mon 14 Sep 2026 that left every user-ordered stems and lyrics
job pending forever in the installed app (``counts.running == 0``, a lyrics
job pending since 06:28Z), and #2610's hydration never wired.

Run in a SUBPROCESS for the same reason as ``test_data_dir_sandbox``: the
legacy modules resolve their paths from MDT_DATA_DIR at import time.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import json
import os
import threading
from pathlib import Path

import apps.webui.server.app as legacy_app

hydration_requests = []
real_bind = legacy_app._bind_stem_hydration


def spy_bind(app, *, data_dir, enabled):
    hydration_requests.append(enabled)
    return real_bind(app, data_dir=data_dir, enabled=enabled)


legacy_app._bind_stem_hydration = spy_bind

from starlette.testclient import TestClient

from apps.engine_core.app import create_app
from apps.engine_core.config import EngineConfig

app = create_app(EngineConfig(data_dir=Path(os.environ["MDT_DATA_DIR"])))
# SEC-01 (#2689): this runs in a bare subprocess (see the module docstring),
# so it never imports tests/conftest.py's TestClient default -- base_url
# must be explicit here or the daemon host allowlist 403s every request.
with TestClient(app, base_url="http://127.0.0.1") as client:
    watcher = app.state.library_jobs_watcher
    thread = watcher._thread
    drain_alive = thread is not None and thread.is_alive()
    thread_names = sorted(t.name for t in threading.enumerate())
    stems_miss = client.get("/api/v1/tracks/odj-probe-no-bundle/stems")
    stems_miss_body = stems_miss.json()

print(json.dumps({
    "stems_miss_status": stems_miss.status_code,
    "stems_miss_code": stems_miss_body.get("code") or stems_miss_body.get("detail", {}).get("code"),
    "hydration_armed": app.state.stem_hydration_s3 is not None,
    "jobs_enabled": app.state.auto_user_jobs.enabled,
    "drain_alive": drain_alive,
    "thread_names": thread_names,
    "hydration_requests": hydration_requests,
}))
"""

DRAIN_THREAD_NAME: str = "webui.library-jobs"


def _run_probe(tmp_path: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    data_dir = tmp_path / "engine-data"
    home_dir = tmp_path / "engine-home"
    data_dir.mkdir()
    home_dir.mkdir()
    env = dict(os.environ)
    env.pop("MUSIC_DJ_LIBRARY_JOBS", None)
    env.pop("WEB_CONCURRENCY", None)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "MDT_DATA_DIR": str(data_dir),
            "MDT_LIBRARY_MODE": "local",
            "HOME": str(home_dir),
            **extra_env,
        }
    )
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=180,
        check=False,
    )


def _probe_payload(result: subprocess.CompletedProcess[str]) -> dict:
    if result.returncode != 0:
        raise AssertionError(
            f"engine probe failed (exit {result.returncode})\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-4000:]}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def default_probe(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return _probe_payload(_run_probe(tmp_path_factory.mktemp("default"), {}))


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
def test_engine_runs_the_library_jobs_drain_by_default(default_probe: dict) -> None:
    """[if] library jobs is left unset [then] the engine boots the drain thread, [else stop]."""
    assert default_probe["jobs_enabled"] is True
    assert default_probe["drain_alive"] is True
    assert DRAIN_THREAD_NAME in default_probe["thread_names"]


@pytest.mark.requirement("STEM-15")
def test_engine_requests_stem_hydration(default_probe: dict) -> None:
    """[if] the packaged engine boots [then] it requests stem hydration once, [else stop]."""
    assert default_probe["hydration_requests"] == [True]


@pytest.mark.requirement("PERFBATCH-05")
def test_library_jobs_off_starts_no_drain(tmp_path: Path) -> None:
    """[if] MUSIC_DJ_LIBRARY_JOBS=off [then] the engine boots with no drain thread, [else stop]."""
    payload = _probe_payload(_run_probe(tmp_path, {"MUSIC_DJ_LIBRARY_JOBS": "off"}))
    assert payload["jobs_enabled"] is False
    assert payload["drain_alive"] is False
    assert DRAIN_THREAD_NAME not in payload["thread_names"]


@pytest.mark.requirement("STEM-15")
def test_cloud_mode_without_boto3_still_boots(tmp_path: Path) -> None:
    """Regression, Mon 14 Sep 2026: the installed app (cloud mode, R2
    credentials resolving, no boto3 in the packaged closure) crashed at boot
    with AssetStoreError from _bind_stem_hydration. The engine must boot,
    run the drain, and leave hydration unarmed with a loud warning.

    [if] R2 resolves but boto3 is absent [then] the engine boots unarmed, not crashed, [else stop].
    """
    shim = tmp_path / "no-boto3"
    (shim / "boto3").mkdir(parents=True)
    (shim / "boto3" / "__init__.py").write_text(
        'raise ImportError("simulated: boto3 is absent from the packaged closure")\n',
        encoding="utf-8",
    )
    result = _run_probe(
        tmp_path,
        {
            "PYTHONPATH": f"{shim}{os.pathsep}{REPO_ROOT}",
            "MUSIC_DJ_CLOUDSYNC_MODE": "cloud",
            "R2_ACCOUNT_ID": "test-account",
            "R2_ACCESS_KEY_ID": "test-key-id",
            "R2_SECRET_ACCESS_KEY": "test-secret",
        },
    )
    payload = _probe_payload(result)
    assert payload["drain_alive"] is True
    assert payload["hydration_armed"] is False
    assert "stem-hydration: R2 credentials resolve but hydration is NOT armed" in result.stderr
    # Unarmed-while-configured must fail loud on a stems miss, never read as
    # the ordinary "no bundle anywhere" empty state.
    assert payload["stems_miss_status"] == 502
    assert payload["stems_miss_code"] == "STEM_HYDRATION_NOT_ARMED"


@pytest.mark.requirement("STEM-15")
def test_local_mode_stems_miss_stays_the_ordinary_empty_state(default_probe: dict) -> None:
    """Opposite-direction control: with hydration legitimately unconfigured
    (local mode), a miss is still the HTTP 200 unavailable empty state.

    [if] hydration is legitimately unconfigured [then] a stems miss stays HTTP 200, [else stop].
    """
    assert default_probe["stems_miss_status"] == 200
    assert default_probe["stems_miss_code"] == "STEM_BUNDLE_NOT_FOUND"


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
def test_invalid_library_jobs_value_refuses_to_boot(tmp_path: Path) -> None:
    """[if] MUSIC_DJ_LIBRARY_JOBS is invalid [then] the engine refuses to boot, [else stop]."""
    result = _run_probe(tmp_path, {"MUSIC_DJ_LIBRARY_JOBS": "maybe"})
    assert result.returncode != 0
    assert "MUSIC_DJ_LIBRARY_JOBS='maybe' is not a member of" in result.stderr
