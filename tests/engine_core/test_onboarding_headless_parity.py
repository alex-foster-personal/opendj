"""SETUP-24: the onboarding gauntlet's headless twin, driven by ``opendj api``.

The browser gauntlet (apps/webui/frontend/tests/e2e/onboarding-gauntlet-*.spec.ts)
walks a new user through the wizard. This walks an AGENT through the same
first run, in the same order, against the same kind of engine: a real
``apps.engine_core serve`` process on a truly empty data dir, a sandboxed
HOME with no rekordbox in it, and the packaged build identity
(``OPENDJ_PAYLOAD_MANIFEST``), so ``should_show_wizard`` is the installed
app's answer and not a checkout's. Every request goes through the shipped
``opendj api`` client (:func:`apps.opendj_cli.api_cli.run`) over a real
socket, so a step the CLI cannot reach is a parity gap that reds here.

Beside test_first_import_visible.py (SETUP-20), which drives the same import
through an in-process TestClient to pin the backend choice; this one pins
the agent-facing contract end to end.

Single-line intent:
  - [if] a fresh packaged engine's status does not ask for the wizard [then] fail, [else stop].
  - [if] detection with no rekordbox lacks rekordbox_not_found [then] fail, [else stop].
  - [if] the user's ~/Music is not offered as a candidate folder [then] fail, [else stop].
  - [if] a missing folder is queued for import, not refused with a code [then] fail, [else stop].
  - [if] the folder import fails or lands fewer tracks than files [then] fail, [else stop].
  - [if] post-import status still shows the wizard or miscounts [then] fail, [else stop].
  - [if] dismissing the wizard over the API does not stick [then] fail, [else stop].
"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from apps.opendj_cli import EXIT_CONFLICT, EXIT_OK, api_cli
from apps.webui.port_config import BACKEND_ENV, FRONTEND_ENV

pytestmark = pytest.mark.requirement("SETUP-24")

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKS = 5
BOOT_TIMEOUT_S = 120.0
IMPORT_TIMEOUT_S = 150.0
TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}

#: The identity keys apps/engine_core/build_info.py requires of a manifest.
TEST_BUILD_IDENTITY = {
    "built_at_utc": "2026-10-01T00:00:00Z",
    "git_branch": "onboarding-gauntlet",
    "git_dirty": False,
    "git_sha": "gauntlet",
    "git_sha_full": "onboarding-gauntlet-test-build",
    "lane_label": "onboarding-gauntlet",
}


@dataclass(frozen=True)
class Engine:
    port: int
    data_dir: Path
    music: Path
    log: Path


@dataclass(frozen=True)
class Answer:
    exit_code: int
    body: Any


# ----- helpers ----------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_wav(path: Path, seconds: float = 0.5) -> None:
    """A real, playable wav: the worker's playability gate reads it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(44100 * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))


def _wait_healthy(port: int, process: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"engine exited {process.returncode}:\n{log.read_text()[-4000:]}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/v1/health", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.25)
    raise AssertionError(f"engine not healthy after {BOOT_TIMEOUT_S}s:\n{log.read_text()[-4000:]}")


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    root = tmp_path_factory.mktemp("onboarding-parity")
    data_dir, home = root / "data", root / "home"
    music = home / "Music" / "My Crate"
    for index in range(1, TRACKS + 1):
        _write_wav(music / f"Parity Track {index:02d}.wav")
    data_dir.mkdir()
    assert not any(data_dir.iterdir()), "a fresh install starts from an empty data dir"
    manifest = root / "payload" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"identity": TEST_BUILD_IDENTITY}), encoding="utf-8")

    port = _free_port()
    log = root / "engine.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "MDT_DATA_DIR": str(data_dir),
        "MDT_LIBRARY_MODE": "local",
        "OPENDJ_PAYLOAD_MANIFEST": str(manifest),
        "OPENDJ_TELEMETRY": "0",
        "PYTHONPATH": str(REPO_ROOT),
    }
    env.pop("WEB_CONCURRENCY", None)
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "apps.engine_core",
                "serve",
                "--data-dir",
                str(data_dir),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        _wait_healthy(port, process, log)
        yield Engine(port=port, data_dir=data_dir, music=music, log=log)
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=30)


def _api(engine: Engine, capsys: pytest.CaptureFixture[str], *argv: str) -> Answer:
    """One ``opendj api`` call against this engine; stdout parsed as JSON."""
    capsys.readouterr()
    exit_code = api_cli.run(
        list(argv),
        environ={BACKEND_ENV: str(engine.port), FRONTEND_ENV: str(_free_port())},
    )
    out, err = capsys.readouterr()
    text = out if exit_code == EXIT_OK else err
    return Answer(exit_code, json.loads(text) if text.strip() else None)


# ----- the first run, as an agent drives it --------------------------------------
def test_a_headless_first_run_matches_the_wizard_step_for_step(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    build = _api(engine, capsys, "GET", "/api/v1/build-info")
    assert build.body["source"] == "payload", build

    status = _api(engine, capsys, "GET", "/api/v1/setup/status")
    assert status.exit_code == EXIT_OK, status
    assert status.body["library_empty"] is True
    assert status.body["dev_mode"] is False
    assert status.body["should_show_wizard"] is True, status.body

    rekordbox = _api(engine, capsys, "GET", "/api/v1/setup/detect/rekordbox")
    assert rekordbox.body["installed"] is False
    assert "rekordbox_not_found" in rekordbox.body["blockers"], rekordbox.body

    permissions = _api(engine, capsys, "GET", "/api/v1/setup/permissions")
    assert permissions.exit_code == EXIT_OK, permissions

    candidates = _api(engine, capsys, "GET", "/api/v1/setup/detect/music-folders")
    offered = {row["path"] for row in candidates.body["candidates"] if row["readable"]}
    assert str(engine.music.parent) in offered, candidates.body

    scan = _api(engine, capsys, "GET", f"/api/v1/setup/detect/folder?path={quote(str(engine.music))}")
    assert scan.body["readable"] is True and scan.body["audio_files"] == TRACKS, scan.body

    queued = _api(
        engine,
        capsys,
        "POST",
        "/api/v1/setup/import/folder",
        "--json",
        json.dumps({"folders": [str(engine.music)]}),
    )
    assert queued.exit_code == EXIT_OK, queued
    job_id = queued.body["id"]

    deadline = time.monotonic() + IMPORT_TIMEOUT_S
    job = _api(engine, capsys, "GET", f"/api/v1/jobs/{job_id}").body
    while job["status"] not in TERMINAL and time.monotonic() < deadline:
        time.sleep(0.25)
        job = _api(engine, capsys, "GET", f"/api/v1/jobs/{job_id}").body
    assert job["status"] == "succeeded", job

    after = _api(engine, capsys, "GET", "/api/v1/setup/status").body
    assert after["should_show_wizard"] is False, after
    assert after["tracks"] == TRACKS, after
    assert after["last_import"]["kind"] == "folder"
    assert after["last_import"]["tracks_written"] == TRACKS, after["last_import"]

    tracks = _api(engine, capsys, "GET", "/api/v1/tracks?limit=500").body
    assert len(tracks["items"]) == TRACKS, tracks

    stems = _api(engine, capsys, "GET", "/api/v1/setup/stems")
    assert stems.exit_code == EXIT_OK and isinstance(stems.body["available"], bool), stems

    dismissed = _api(engine, capsys, "POST", "/api/v1/setup/dismiss", "--json", json.dumps({"dismissed": True}))
    assert dismissed.body["dismissed"] is True, dismissed
    assert _api(engine, capsys, "GET", "/api/v1/setup/status").body["dismissed"] is True


def test_a_missing_folder_is_refused_with_a_code_before_any_job(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = engine.music.parent / "Typo Crate That Was Never Made"
    scan = _api(engine, capsys, "GET", f"/api/v1/setup/detect/folder?path={quote(str(missing))}")
    assert scan.body["exists"] is False and scan.body["audio_files"] == 0, scan.body

    jobs_before = _api(engine, capsys, "GET", "/api/v1/jobs?limit=200").body
    refused = _api(
        engine,
        capsys,
        "POST",
        "/api/v1/setup/import/folder",
        "--json",
        json.dumps({"folders": [str(missing)]}),
    )
    assert refused.exit_code == EXIT_CONFLICT, refused
    assert refused.body["detail"]["code"], refused.body
    jobs_after = _api(engine, capsys, "GET", "/api/v1/jobs?limit=200").body
    assert len(jobs_after["items"] if isinstance(jobs_after, dict) else jobs_after) == len(
        jobs_before["items"] if isinstance(jobs_before, dict) else jobs_before
    ), "a refused import must not enqueue a job"
