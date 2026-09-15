"""Stem generation resolves the same real crate path as audio playback."""

import json
import sqlite3
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.stems.cli import resolve_audio_path
from apps.stems.tiers import get_tier
from apps.webui.server.routes.stem_tiers import _generate_command


def test_resolve_audio_path_uses_the_data_dirs_real_path_map(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    source_root = Path("/Users/user/Music")
    crate_root = tmp_path / "crate"
    audio = crate_root / "Track.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"real-audio-bytes")

    with sqlite3.connect(state_dir / "state.db") as conn:
        conn.execute(
            "CREATE TABLE tracks (stable_id TEXT, file_path TEXT, deleted_at TEXT)"
        )
        conn.execute(
            "INSERT INTO tracks (stable_id, file_path) VALUES (?, ?)",
            ("track-remote", str(source_root / "Track.mp3")),
        )
    (data_dir / "path-map.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"from": str(source_root), "to": str(crate_root)}
                ]
            }
        ),
        encoding="utf-8",
    )

    assert resolve_audio_path(data_dir, "track-remote") == audio


def test_resolve_audio_path_prefers_local_track_location(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    local = tmp_path / "mirror.mp3"
    local.write_bytes(b"real-audio-bytes")
    sid = "a" * 40

    conn = state_db.open_rw(state_dir / "state.db")
    writer = StateWriter(conn, actor="test-stems")
    try:
        writer.upsert_track(
            stable_id=sid,
            stable_id_tier="inferred",
            title="mirrored",
            artists=["X"],
            album=None,
            isrc=None,
            duration_ms=1000,
            file_path=str(local),
        )
        conn.execute(
            "UPDATE tracks SET file_path = ? WHERE stable_id = ?",
            ("/Users/dev/Music/ghost.mp3", sid),
        )
        conn.commit()
    finally:
        writer.close()
        conn.close()

    assert resolve_audio_path(data_dir, sid) == local


def test_local_generate_command_targets_one_bundle_not_the_store_root(
    tmp_path: Path,
) -> None:
    stems_root = tmp_path / "remote-stems"

    command = _generate_command(
        get_tier("LOCAL"),
        "track-remote",
        "/crate/Track.mp3",
        stems_dir=stems_root,
    )

    assert command[-2:] == [
        "--out-dir",
        str(stems_root / "track-remote"),
    ]
