"""REC CLI preserves daemon non-2xx details for every subcommand (issue #12412).

[if] a REC subcommand hits the daemon [then] stderr keeps status and detail, [else stop].
[if] the daemon is unreachable [then] stderr says unreachable, [else stop].
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from apps.sets import record as record_mod
from apps.sets.recorder_service import RecorderService
from apps.sets.state import SetsState
from tests.sets.conftest import mount_sets_router
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread

pytestmark = pytest.mark.requirement("SET-12")

START_JSON = {"session_id": None, "source": "none", "sources": []}


def run_rec(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "apps.sets", "rec", *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def _unused_base_url() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def sets_base_url(tmp_path: Path) -> Iterator[tuple[str, RecorderService]]:
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    mount_sets_router(app)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"),
        what="sets rec cli errors",
        sockets=[listener],
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, service
    finally:
        stop_uvicorn_in_thread(server, thread, what="sets rec cli errors")


def test_rec_status_succeeds_on_idle_daemon(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] the daemon is idle [then ⛔️] rec status exits zero with JSON on stdout."""
    base_url, _ = sets_base_url
    result = run_rec(["status", "--base-url", base_url])
    assert result.returncode == 0, result.stderr
    assert '"active": false' in result.stdout


def test_start_conflict_preserves_daemon_detail(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] start receives a conflict response [then ⛔️] stderr keeps 409 and the reason."""
    import urllib.request

    base_url, _ = sets_base_url
    urllib.request.urlopen(
        urllib.request.Request(
            f"{base_url}/api/sets/recorder/start",
            data=json.dumps(START_JSON).encode(),
            method="POST",
            headers={"content-type": "application/json"},
        ),
        timeout=30,
    ).read()
    result = run_rec(["start", "--base-url", base_url, "--source", "none", "--sources"])
    assert result.returncode != 0
    assert "409" in result.stderr
    assert "already" in result.stderr.lower()


def test_stop_missing_session_preserves_daemon_detail(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] stop receives a missing-session response [then ⛔️] stderr keeps 409 and session id."""
    import urllib.request

    base_url, _ = sets_base_url
    session_id = "2026-07-22T22-10-00"
    urllib.request.urlopen(
        urllib.request.Request(
            f"{base_url}/api/sets/recorder/start",
            data=json.dumps({"session_id": session_id, "source": "none", "sources": []}).encode(),
            method="POST",
            headers={"content-type": "application/json"},
        ),
        timeout=30,
    ).read()
    wrong_id = "2026-07-22T22-11-00"
    result = run_rec(["stop", "--base-url", base_url, "--session-id", wrong_id])
    assert result.returncode != 0
    assert "409" in result.stderr
    assert session_id in result.stderr


def test_recover_live_pid_preserves_daemon_detail(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] recovery receives a live-PID refusal [then ⛔️] stderr keeps 409 and still running."""
    base_url, service = sets_base_url
    session_id = "2026-07-22T22-40-00"
    record_mod.start(
        session_id=session_id,
        config=record_mod.RecorderConfig(sources=(), capture_disabled=True),
        sets_root=service.sets_root,
        state=SetsState(db_path=service.db_path),
    )
    result = run_rec(
        [
            "recover",
            "--base-url",
            base_url,
            "--session-id",
            session_id,
            "--expected-pid",
            str(os.getpid()),
        ]
    )
    assert result.returncode != 0
    assert "409" in result.stderr
    assert "still running" in result.stderr


def test_timeline_missing_session_preserves_daemon_detail(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] timeline names a missing session [then ⛔️] stderr keeps 404 and session not found."""
    base_url, _ = sets_base_url
    result = run_rec(["timeline", "--base-url", base_url, "--session-id", "nope"])
    assert result.returncode != 0
    assert "404" in result.stderr
    assert "session not found" in result.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--base-url", "BASE"],
        ["list", "--base-url", "BASE"],
        ["timeline", "--base-url", "BASE", "--session-id", "nope"],
        ["start", "--base-url", "BASE", "--source", "none", "--sources"],
        ["stop", "--base-url", "BASE"],
        [
            "recover",
            "--base-url",
            "BASE",
            "--session-id",
            "nope",
            "--expected-pid",
            "1",
        ],
    ],
)
def test_unreachable_daemon_preserves_reason(argv: list[str]) -> None:
    """[if] the daemon is unreachable [then ⛔️] rec exits nonzero with unreachable on stderr."""
    base = _unused_base_url()
    resolved = [base if part == "BASE" else part for part in argv]
    result = run_rec(resolved)
    assert result.returncode != 0
    assert "unreachable" in result.stderr
