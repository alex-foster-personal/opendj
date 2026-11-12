"""REC CLI preserves daemon non-2xx details for every subcommand (issues #12412, #13161, #13245).

[if] a REC subcommand hits the daemon [then] stderr keeps status and detail, [else stop].
[if] the daemon is unreachable [then] stderr says unreachable, [else stop].
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

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
        timeout=60,
    )


def _post_json(base_url: str, path: str, body: dict) -> dict:
    with urllib.request.urlopen(
        urllib.request.Request(
            base_url.rstrip("/") + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"content-type": "application/json"},
        ),
        timeout=30,
    ) as response:
        return json.loads(response.read() or b"null")


def _start_opendj_recording(base_url: str) -> str:
    status = _post_json(
        base_url,
        "/api/sets/recorder/start",
        {"session_id": None, "source": "none", "sources": ["opendj_decks"]},
    )
    return status["session_id"]


def _unused_base_url() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def recorder_requests() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def after_status() -> list[Callable[[], None]]:
    return []


@pytest.fixture
def sets_base_url(
    tmp_path: Path, recorder_requests: list[tuple[str, str]], after_status: list[Callable[[], None]]
) -> Iterator[tuple[str, RecorderService]]:
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
    )
    app = FastAPI()

    @app.middleware("http")
    async def observe_requests(request: Request, call_next: RequestResponseEndpoint) -> Response:
        recorder_requests.append((request.method, request.url.path))
        response = await call_next(request)
        if request.method == "GET" and request.url.path == "/api/sets/recorder" and after_status:
            after_status.pop(0)()
        return response

    app.state.sets_recorder_service = service
    app.state.sets_root = service.sets_root
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


def test_rec_sessions_empty_store_returns_empty_array(
    sets_base_url: tuple[str, RecorderService],
) -> None:
    """[if] no session is stored [then ⛔️] rec sessions exits 0 with [] on stdout."""
    base_url, _ = sets_base_url
    result = run_rec(["sessions", "--base-url", base_url])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert result.stderr == ""


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
    """[if] status reports a live owner [then] recovery names that owner, [else stop]."""
    base_url, service = sets_base_url
    session_id = "2026-07-22T22-40-00"
    record_mod.start(
        session_id=session_id,
        config=record_mod.RecorderConfig(sources=(), capture_disabled=True),
        sets_root=service.sets_root,
        state=SetsState(db_path=service.db_path),
    )
    result = run_rec(["recover", "--base-url", base_url])
    assert result.returncode != 0
    assert str(os.getpid()) in result.stderr
    assert session_id in result.stderr


def test_timeline_missing_session_preserves_daemon_detail(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] timeline names a missing session [then ⛔️] stderr keeps 404 and session not found."""
    base_url, _ = sets_base_url
    result = run_rec(["timeline", "--base-url", base_url, "--session-id", "nope"])
    assert result.returncode != 0
    assert "404" in result.stderr
    assert "session not found" in result.stderr


@pytest.mark.requirement("SET-12828")
def test_observations_active_recording_without_posts_reports_null_receipt(
    sets_base_url: tuple[str, RecorderService],
) -> None:
    """[if] recording runs and no snapshots posted [then] stdout has null receipt, [else stop]."""
    base_url, _ = sets_base_url
    _start_opendj_recording(base_url)
    result = run_rec(["observations", "--base-url", base_url])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"] == "opendj_decks"
    assert payload["last_received_at"] is None
    assert payload.get("last_error") is None


@pytest.mark.requirement("SET-12828")
def test_observations_after_post_includes_parseable_last_received_at(
    sets_base_url: tuple[str, RecorderService],
) -> None:
    """[if] a snapshot was posted [then] stdout last_received_at parses, [else stop]."""
    base_url, _ = sets_base_url
    session_id = _start_opendj_recording(base_url)
    observed_at = datetime(2026, 8, 31, 20, 0, 0, tzinfo=UTC).isoformat(timespec="milliseconds")
    _post_json(
        base_url,
        "/api/sets/deck-observations",
        {
            "session_id": session_id,
            "snapshots": [
                {
                    "observed_at": observed_at,
                    "decks": {
                        "1": {
                            "stable_id": "trk-a",
                            "playing": True,
                            "audible": True,
                            "position_ms": 0.0,
                        }
                    },
                }
            ],
        },
    )
    result = run_rec(["observations", "--base-url", base_url])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["last_received_at"] is not None
    datetime.fromisoformat(payload["last_received_at"].replace("Z", "+00:00"))


@pytest.mark.requirement("SET-12828")
def test_observations_idle_daemon_preserves_409_detail(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] no live observer [then] observations exits 1 with 409 on stderr, [else stop]."""
    base_url, _ = sets_base_url
    result = run_rec(["observations", "--base-url", base_url])
    assert result.returncode != 0
    assert "409" in result.stderr


@pytest.mark.requirement("SET-12826")
def test_transitions_matches_http_route(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] session has transitions [then ⛔️] rec stdout matches GET /transitions."""
    import urllib.request

    from tests.sets.test_api import _seed_api_session

    base_url, service = sets_base_url
    session_id = "rec-cli-t1"
    _seed_api_session(service.sets_root, session_id=session_id)
    http = urllib.request.urlopen(f"{base_url}/api/sets/{session_id}/transitions", timeout=30)
    expected = json.loads(http.read())
    result = run_rec(["transitions", "--base-url", base_url, "--session-id", session_id])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


@pytest.mark.requirement("SET-12826")
def test_transitions_empty_array_when_no_jsonl(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] session has no transitions file [then ⛔️] stdout is [] and exit 0."""
    from apps.sets.manifest import AudioSegment, Manifest, write_manifest

    base_url, service = sets_base_url
    session_id = "rec-cli-empty"
    sess = service.sets_root / session_id
    sess.mkdir(parents=True)
    mp3 = sess / "audio.wav"
    mp3.write_bytes(b"\xff\xfb" + b"\x00" * 64)
    write_manifest(
        sess,
        Manifest(
            session_id=session_id,
            started_at="2026-04-17T21:30:00+00:00",
            ended_at="2026-04-17T23:00:00+00:00",
            capture_device="test",
            share_state="private",
            event_count=0,
            deck_sources=[],
            mp3_segments=[AudioSegment(name=mp3.name, start_t_s=0.0, duration_s=1.0, size_bytes=mp3.stat().st_size)],
        ),
    )
    result = run_rec(["transitions", "--base-url", base_url, "--session-id", session_id])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


@pytest.mark.requirement("SET-12826")
def test_transitions_missing_session_preserves_daemon_detail(sets_base_url: tuple[str, RecorderService]) -> None:
    """[if] transitions names a missing session [then ⛔️] stderr keeps 404 and session not found."""
    base_url, _ = sets_base_url
    result = run_rec(["transitions", "--base-url", base_url, "--session-id", "nope"])
    assert result.returncode != 0
    assert "404" in result.stderr
    assert "session not found" in result.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--base-url", "BASE"],
        ["observations", "--base-url", "BASE"],
        ["sessions", "--base-url", "BASE"],
        ["remembered-input", "--base-url", "BASE"],
        ["list", "--base-url", "BASE"],
        ["timeline", "--base-url", "BASE", "--session-id", "nope"],
        ["transitions", "--base-url", "BASE", "--session-id", "nope"],
        ["start", "--base-url", "BASE", "--source", "none", "--sources"],
        ["stop", "--base-url", "BASE"],
        ["recover", "--base-url", "BASE"],
    ],
)
def test_unreachable_daemon_preserves_reason(argv: list[str]) -> None:
    """[if] the daemon is unreachable [then ⛔️] rec exits nonzero with unreachable on stderr."""
    base = _unused_base_url()
    resolved = [base if part == "BASE" else part for part in argv]
    result = run_rec(resolved)
    assert result.returncode != 0
    assert "unreachable" in result.stderr


@pytest.mark.requirement("SET-12844")
@pytest.mark.parametrize("entrypoint", ["sets", "opendj"])
@pytest.mark.parametrize("owner", ["stale", "live", "idle", "changed"])
def test_recovery_over_real_http(
    sets_base_url: tuple[str, RecorderService],
    recorder_requests: list[tuple[str, str]],
    after_status: list[Callable[[], None]],
    tmp_path: Path,
    entrypoint: str,
    owner: str,
) -> None:
    """[if] either CLI recovers [then] the real daemon enforces owner safety, [else stop]."""
    base_url, service = sets_base_url
    session_id = "2026-07-22T22-50-00"
    session_dir = service.sets_root / session_id
    if owner != "idle":
        record_mod.start(
            session_id=session_id,
            config=record_mod.RecorderConfig(sources=(), capture_disabled=True),
            sets_root=service.sets_root,
            state=SetsState(db_path=service.db_path),
        )
        if owner in ("stale", "changed"):
            # A genuinely exited child leaves a dead PID, not a simulated liveness answer.
            child = subprocess.Popen([sys.executable, "-c", "pass"])
            assert child.wait(timeout=10) == 0
            (session_dir / "recorder.pid").write_text(str(child.pid), encoding="utf-8")
        if owner == "changed":

            def replace_owner() -> None:
                (session_dir / "recorder.pid").write_text(str(os.getpid()), encoding="utf-8")

            after_status.append(replace_owner)
    if entrypoint == "sets":
        result = run_rec(["recover", "--base-url", base_url])
    else:
        lock = tmp_path / "engine.lock"
        lock.write_text(
            json.dumps({"host": "127.0.0.1", "port": int(base_url.rsplit(":", 1)[1]), "pid": os.getpid()}),
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, "-m", "apps.opendj_cli", "--lock", str(lock), "rec", "recover"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    assert result.returncode == (0 if owner == "stale" else 1), result.stderr
    expected = [("GET", "/api/sets/recorder")]
    if owner in ("stale", "changed"):
        expected.append(("POST", f"/api/sets/recorder/{session_id}/recover"))
    assert recorder_requests == expected
    if owner == "stale":
        assert json.loads(result.stdout)["active"] is False
        assert not (session_dir / "recorder.pid").exists()
        assert (session_dir / "manifest.json").exists()
        assert "session_end" in (session_dir / "timeline.jsonl").read_text(encoding="utf-8")
    elif owner == "live":
        assert str(os.getpid()) in result.stderr and session_id in result.stderr
    elif owner == "idle":
        assert "nothing to recover" in result.stderr
    elif owner == "changed":
        assert '409 {"detail":"recorder ownership changed; refresh before recovery"}' in result.stderr
        assert (session_dir / "recorder.pid").read_text(encoding="utf-8") == str(os.getpid())
        assert not (session_dir / "manifest.json").exists()
        assert "session_end" not in (session_dir / "timeline.jsonl").read_text(encoding="utf-8")


@pytest.mark.requirement("SET-10")
@pytest.mark.requirement("SET-12842")
@pytest.mark.parametrize(
    "choice", [None, {"kind": "device", "name": "BlackHole 2ch"}, {"kind": "none"}, {"kind": "master"}]
)
def test_remembered_input_matches_daemon(
    sets_base_url: tuple[str, RecorderService],
    choice: dict[str, str] | None,
) -> None:
    """[if] REC has a choice or none [then] CLI prints daemon JSON and exits zero, [else stop]."""
    base_url, service = sets_base_url
    if choice is not None:
        service._remember_input(choice)
    response = httpx.get(base_url + "/api/sets/recorder/remembered-input")
    assert response.status_code == 200
    result = run_rec(["remembered-input", "--base-url", base_url])
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload == response.json()
    if choice is None:
        assert payload == {"remembered": None}
    elif choice["kind"] == "device":
        assert payload["remembered"]["name"] == "BlackHole 2ch"
    else:
        assert payload == {"remembered": {"kind": choice["kind"], "name": None}}


@pytest.mark.requirement("SET-10")
@pytest.mark.requirement("SET-12842")
@pytest.mark.parametrize("unreadable", [False, True])
def test_remembered_input_preserves_store_errors(
    sets_base_url: tuple[str, RecorderService],
    unreadable: bool,
) -> None:
    """[if] the REC store cannot be read [then] CLI preserves its 500 detail, [else stop]."""
    base_url, service = sets_base_url
    service.remembered_input_path.parent.mkdir(parents=True, exist_ok=True)
    if unreadable:
        service.remembered_input_path.mkdir()
    else:
        service.remembered_input_path.write_text("not json", encoding="utf-8")
    response = httpx.get(base_url + "/api/sets/recorder/remembered-input")
    assert response.status_code == 500
    result = run_rec(["remembered-input", "--base-url", base_url])
    assert result.returncode == 1
    assert result.stdout == ""
    assert "500" in result.stderr
    assert json.loads(result.stderr.split(": 500 ", 1)[1]) == response.json()
