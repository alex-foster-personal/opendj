"""REC CLI lists sessions and prints validated timeline NDJSON (issue #12400).

[if] completed sessions exist [then] rec sessions lists them, [else stop].
[if] timeline NDJSON is malformed [then] rec timeline fails on stderr, [else stop].
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from apps.sets.manifest import AudioSegment, Manifest, write_manifest
from apps.sets.recorder_service import RecorderService
from tests.sets.conftest import mount_sets_router
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread

pytestmark = pytest.mark.requirement("SETREC-19")

SESSION_ID = "2026-04-17T21-30-00"


def run_rec(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "apps.sets", "rec", *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def _seed_completed_session(sets_root: Path, session_id: str = SESSION_ID) -> Path:
    sess = sets_root / session_id
    sess.mkdir(parents=True, exist_ok=True)
    mp3 = sess / "audio_2026-04-17T21-30-00.mp3"
    mp3.write_bytes(b"\xff\xfb" + b"\x00" * 1024)
    write_manifest(
        sess,
        Manifest(
            session_id=session_id,
            started_at="2026-04-17T21:30:00+00:00",
            ended_at="2026-04-17T23:00:00+00:00",
            capture_device="BlackHole 2ch",
            share_state="private",
            event_count=2,
            deck_sources=["djay_monitor"],
            mp3_segments=[
                AudioSegment(
                    name=mp3.name,
                    start_t_s=0.0,
                    duration_s=300.0,
                    size_bytes=mp3.stat().st_size,
                )
            ],
        ),
    )
    timeline = [
        {
            "session_id": session_id,
            "timestamp_s": 0.0,
            "wall_clock": "2026-04-17T21:30:00+00:00",
            "action": "session_start",
            "source": "recorder",
            "deck": None,
            "track_stable_id": None,
            "value": {},
        },
        {
            "session_id": session_id,
            "timestamp_s": 5.0,
            "wall_clock": "2026-04-17T21:30:05+00:00",
            "action": "track_loaded",
            "source": "djay_monitor",
            "deck": "A",
            "track_stable_id": "uuid-1",
            "value": {"title": "Track 1"},
        },
    ]
    (sess / "timeline.jsonl").write_text("\n".join(json.dumps(t) for t in timeline) + "\n")
    return sess


@pytest.fixture
def sets_read_base_url(tmp_path: Path, sets_state_db: Path) -> Iterator[str]:
    sets_dir = tmp_path / "sets"
    service = RecorderService(
        sets_root=sets_dir,
        db_path=sets_dir / "sets.db",
        capture_enabled=False,
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    app.state.sets_root = sets_dir
    app.state.state_db_path = str(sets_state_db)
    mount_sets_router(app)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"),
        what="sets rec cli reads",
        sockets=[listener],
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url
    finally:
        stop_uvicorn_in_thread(server, thread, what="sets rec cli reads")


def test_rec_sessions_lists_completed_recordings(sets_read_base_url: str, tmp_path: Path) -> None:
    """[if] completed recordings exist [then ⛔️] rec sessions exits 0 with the session id."""
    sets_dir = tmp_path / "sets"
    _seed_completed_session(sets_dir)
    result = run_rec(["sessions", "--base-url", sets_read_base_url])
    assert result.returncode == 0, result.stderr
    sessions = json.loads(result.stdout)
    assert any(row.get("session_id") == SESSION_ID for row in sessions)
    match = next(row for row in sessions if row.get("session_id") == SESSION_ID)
    assert match.get("event_count") == 2


def test_rec_timeline_prints_known_session_events(sets_read_base_url: str, tmp_path: Path) -> None:
    """[if] a session has timeline events [then ⛔️] rec timeline prints valid NDJSON lines."""
    sets_dir = tmp_path / "sets"
    _seed_completed_session(sets_dir)
    result = run_rec(["timeline", "--base-url", sets_read_base_url, "--session-id", SESSION_ID])
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(e.get("action") == "track_loaded" for e in events)


def test_rec_timeline_missing_session_fails(sets_read_base_url: str) -> None:
    """[if] the session does not exist [then ⛔️] rec timeline exits nonzero with 404 detail."""
    result = run_rec(["timeline", "--base-url", sets_read_base_url, "--session-id", "nope"])
    assert result.returncode != 0
    assert "404" in result.stderr
    assert "session not found" in result.stderr


def test_rec_timeline_rejects_malformed_ndjson(sets_read_base_url: str, tmp_path: Path) -> None:
    """[if] the daemon returns malformed timeline bytes [then ⛔️] rec timeline fails before stdout."""
    sets_dir = tmp_path / "sets"
    sess = _seed_completed_session(sets_dir)
    timeline_path = sess / "timeline.jsonl"
    timeline_path.write_text(timeline_path.read_text(encoding="utf-8") + '{"action":"tail"\n', encoding="utf-8")
    result = run_rec(["timeline", "--base-url", sets_read_base_url, "--session-id", SESSION_ID])
    assert result.returncode != 0
    assert "malformed timeline" in result.stderr.lower()
