"""Requirements: LIBUX-07, LIBUX-13.

Remote-audio status is a third bucket, never streaming or awaiting-volume.

[if] a track's audio is held remotely rather than locally [then] is_remote is True, [else stop].
[if] the row is streaming or awaiting-volume [then] is_remote stays False, [else stop].
[if] the audio is local (the common case) [then] is_remote stays False, [else stop].
[if] no remote copy is recorded [then] is_remote is False, [else stop].
[if] the CLI sees a DB with no remote copies [then] it prints an empty list, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.cloud import transfer_status
from apps.shared import remote_status
from apps.shared.state import db as state_db

pytestmark = [pytest.mark.requirement("LIBUX-07"), pytest.mark.requirement("LIBUX-13")]

SID_REMOTE = "a" * 40
SID_LOCAL = "b" * 40


def test_is_remote_audio_true_only_for_our_non_local_copy() -> None:
    assert (
        remote_status.is_remote_audio(
            is_streaming=False,
            is_awaiting_volume=False,
            has_local_audio=False,
            has_remote_copy=True,
        )
        is True
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "is_streaming": True,
            "is_awaiting_volume": False,
            "has_local_audio": False,
            "has_remote_copy": True,
        },
        {
            "is_streaming": False,
            "is_awaiting_volume": True,
            "has_local_audio": False,
            "has_remote_copy": True,
        },
        {
            "is_streaming": False,
            "is_awaiting_volume": False,
            "has_local_audio": True,
            "has_remote_copy": True,
        },
        {
            "is_streaming": False,
            "is_awaiting_volume": False,
            "has_local_audio": False,
            "has_remote_copy": False,
        },
    ],
)
def test_is_remote_audio_stays_quiet_for_every_non_remote_bucket(kwargs: dict) -> None:
    assert remote_status.is_remote_audio(**kwargs) is False


def test_awaiting_volume_is_not_remote() -> None:
    assert remote_status.is_awaiting_volume("/Volumes/SLATER/set/track.flac", mounted=set()) is True
    assert (
        remote_status.is_awaiting_volume("/Volumes/SLATER/set/track.flac", mounted={"SLATER"})
        is False
    )
    assert remote_status.is_awaiting_volume("/Users/maintainer/Music/track.flac", mounted=set()) is False
    assert remote_status.is_awaiting_volume("tidal:123", mounted=set()) is False
    assert remote_status.is_awaiting_volume(None, mounted=set()) is False


def test_sids_with_remote_copy_reads_kind_remote_url_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        writer = _init_track(conn, SID_REMOTE)
        writer.upsert_track_location(
            stable_id=SID_REMOTE,
            kind="remote",
            remote_url="r2://library/audio/" + SID_REMOTE,
        )
        writer.upsert_track(
            stable_id=SID_LOCAL,
            stable_id_tier="inferred",
            title="Local",
            artists=["X"],
            album=None,
            isrc=None,
            duration_ms=180_000,
            file_path=str(tmp_path / "local.flac"),
        )
        writer.upsert_track_location(
            stable_id=SID_LOCAL,
            kind="remote",
            remote_url="r2://library/audio/" + SID_LOCAL,
        )
        writer.close()
        found = remote_status.sids_with_remote_copy(conn, [SID_REMOTE, SID_LOCAL, "e" * 40])
    finally:
        conn.close()
    assert SID_REMOTE in found
    assert SID_LOCAL in found


def test_cli_json_lists_only_recorded_remote_sids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        writer = _init_track(conn, SID_REMOTE)
        writer.upsert_track_location(
            stable_id=SID_REMOTE,
            kind="remote",
            remote_url="r2://library/audio/" + SID_REMOTE,
        )
        writer.close()
    finally:
        conn.close()
    assert remote_status.main(["--state-db", str(db_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_stable_ids"] == [SID_REMOTE]


def test_cli_json_empty_when_no_remote_copies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    conn.close()
    assert remote_status.main(["--state-db", str(db_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_stable_ids"] == []


def test_build_track_rows_sets_is_remote_from_track_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.webui.server import backend
    from apps.webui.server.rb_vendor_pkg import track_rows

    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        writer = _init_track(conn, SID_REMOTE)
        writer.upsert_track_location(
            stable_id=SID_REMOTE,
            kind="remote",
            remote_url="r2://library/audio/" + SID_REMOTE,
        )
        writer.upsert_track(
            stable_id=SID_LOCAL,
            stable_id_tier="inferred",
            title="Local",
            artists=["X"],
            album=None,
            isrc=None,
            duration_ms=180_000,
            file_path=str(tmp_path / "local.flac"),
        )
        writer.upsert_track_location(
            stable_id=SID_LOCAL,
            kind="remote",
            remote_url="r2://library/audio/" + SID_LOCAL,
        )
        writer.close()
    finally:
        conn.close()

    monkeypatch.setattr(track_rows.config, "STATE_DB", db_path)
    tracks = [
        backend.Track(stable_id=SID_REMOTE),
        backend.Track(stable_id=SID_LOCAL),
    ]
    monkeypatch.setattr(track_rows, "bulk_rb_meta", lambda stable_ids: {})
    monkeypatch.setattr(
        track_rows,
        "bulk_availability_status",
        lambda *a, **k: {SID_REMOTE: "absent", SID_LOCAL: "present"},
    )
    monkeypatch.setattr(
        track_rows, "bulk_quality", lambda *a, **k: {t.stable_id: {} for t in tracks}
    )
    monkeypatch.setattr(
        track_rows,
        "bulk_stem_summaries",
        lambda stable_ids: {sid: {} for sid in stable_ids},
    )
    transfer_token = transfer_status.begin_transfer(
        SID_LOCAL, "upload", bytes_total=12
    )
    transfer_status.update_transfer(SID_LOCAL, transfer_token, 3)
    try:
        rows = {row["stable_id"]: row for row in track_rows.build_track_rows(tracks)}
    finally:
        transfer_status.clear_transfer(SID_LOCAL, transfer_token)
    assert rows[SID_REMOTE]["is_remote"] is True
    assert rows[SID_LOCAL]["is_remote"] is False
    assert rows[SID_REMOTE]["has_remote_copy"] is True
    assert rows[SID_LOCAL]["has_remote_copy"] is True
    assert rows[SID_REMOTE]["cloud_transfer"] is None
    assert rows[SID_LOCAL]["cloud_transfer"] == {
        "direction": "upload",
        "bytes_transferred": 3,
        "bytes_total": 12,
    }


def _init_track(conn, stable_id: str):
    from apps.shared.state.writer import StateWriter

    writer = StateWriter(conn, actor="unit-test")
    writer.upsert_track(
        stable_id=stable_id,
        stable_id_tier="inferred",
        title="Remote",
        artists=["X"],
        album=None,
        isrc=None,
        duration_ms=180_000,
        file_path=None,
    )
    return writer
