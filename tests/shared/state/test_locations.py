"""track_locations picker: local>remote, works>broken, venue window."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import locations
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("INFRA-01")

SID = "d" * 40


def _init_track(conn, file_path: str | None = None) -> StateWriter:
    writer = StateWriter(conn, actor="unit-test")
    writer.upsert_track(
        stable_id=SID,
        stable_id_tier="inferred",
        title="Demo",
        artists=["X"],
        album=None,
        isrc=None,
        duration_ms=180_000,
        file_path=file_path,
    )
    return writer


def _flac(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC-not-a-real-frame-but-a-real-file")
    return path


def test_pick_prefers_working_local_over_remote(state_conn, tmp_path: Path) -> None:
    local = _flac(tmp_path / "local.flac")
    remote = _flac(tmp_path / "remote.flac")
    writer = _init_track(state_conn, file_path=str(local))
    try:
        writer.upsert_track_location(
            stable_id=SID, kind="remote", file_path=str(remote),
        )
        picked = locations.pick_playable(state_conn, SID)
    finally:
        writer.close()
    assert picked is not None
    assert picked.path == local
    assert picked.kind == "local"


def test_pick_skips_broken_and_uses_working_alternate(
    state_conn, tmp_path: Path,
) -> None:
    missing = tmp_path / "gone.flac"
    working = _flac(tmp_path / "ok.flac")
    writer = _init_track(state_conn, file_path=str(missing))
    try:
        writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=str(working),
        )
        picked = locations.pick_playable(state_conn, SID)
    finally:
        writer.close()
    assert picked is not None
    assert picked.path == working


def test_pick_share_window_prefers_lossy_when_both_work(
    state_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lossless = _flac(tmp_path / "master.flac")
    lossy = tmp_path / "radio.mp3"
    # 1s at 320 kbps effective -> Warehouse (lossy ceiling).
    lossy.write_bytes(b"ID3" + bytes(40_000))
    writer = StateWriter(state_conn, actor="unit-test")
    writer.upsert_track(
        stable_id=SID,
        stable_id_tier="inferred",
        title="Demo",
        artists=["X"],
        album=None,
        isrc=None,
        duration_ms=1000,
        file_path=str(lossless),
    )
    try:
        writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=str(lossy),
        )
        monkeypatch.setenv("MDT_AUDIO_SHARE_MAX_VENUE", "warehouse")
        picked = locations.pick_playable(
            state_conn, SID, policy=locations.policy_from_env(share=True),
        )
    finally:
        writer.close()
    assert picked is not None
    assert picked.path == lossy
    assert picked.venue_key == "warehouse"


def test_pick_returns_none_when_nothing_works(state_conn, tmp_path: Path) -> None:
    writer = _init_track(state_conn, file_path=str(tmp_path / "missing.flac"))
    try:
        assert locations.pick_playable(state_conn, SID) is None
    finally:
        writer.close()


def test_unknown_venue_env_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDT_AUDIO_MIN_VENUE", "disco")
    with pytest.raises(locations.LocationError, match="unknown venue"):
        locations.policy_from_env()


def test_fresh_db_migrates_locations_table(state_db_path: Path) -> None:
    conn = state_db.open_rw(state_db_path)
    try:
        assert locations.locations_table_ready(conn)
    finally:
        conn.close()
