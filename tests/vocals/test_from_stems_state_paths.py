"""from-stems reads track audio from state.db, never from master.plain.db.

The packaged app has no decrypted rekordbox copy, so a from-stems run that
opened ``master.plain.db`` failed every library refresh with
``MASTER_DB missing on disk``.

[if] there is no master.plain.db [then] from-stems still finds each mapped track's audio, [else stop].

Regression one-liners:
  - if from-stems needs master.plain.db then broken
  - if a rekordbox-mapped track's state.db audio path is not what from-stems sees then broken
  - if an unmapped or deleted track enters from-stems then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.vocals import cli

pytestmark = pytest.mark.requirement("CAT-05")


def _state(data_dir: Path, tmp_path: Path) -> dict[str, Path]:
    """state.db with a mapped track, an unmapped one and a deleted mapped one."""
    (data_dir / "state").mkdir(parents=True)
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    writer = StateWriter(conn, actor="unit-test")
    audio: dict[str, Path] = {}
    for sid in ("mapped", "unmapped", "deleted"):
        path = tmp_path / f"{sid}.wav"
        path.write_bytes(b"RIFF")
        audio[sid] = path
        writer.upsert_track(
            stable_id=sid,
            stable_id_tier="inferred",
            title=sid.title(),
            artists=["X"],
            album=None,
            isrc=None,
            duration_ms=180_000,
            file_path=str(path),
        )
    conn.executemany(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)",
        [("mapped", "1"), ("deleted", "2")],
    )
    conn.execute("UPDATE tracks SET deleted_at = '2026-10-02T00:00:00Z' WHERE stable_id = 'deleted'")
    conn.commit()
    conn.close()
    return audio


def test_state_tracks_come_from_state_db_alone(tmp_path: Path) -> None:
    """[if] there is no master.plain.db [then] mapped live tracks still resolve to their audio, [else stop]."""
    data_dir = tmp_path / "data"
    audio = _state(data_dir, tmp_path)
    assert not (data_dir / "master.plain.db").exists()

    tracks = cli.load_state_tracks(cli.Ctx(data_dir=data_dir))

    assert [t.stable_id for t in tracks] == ["mapped"]
    assert tracks[0].audio_path == audio["mapped"]
    assert tracks[0].audio_on_disk is True


def test_from_stems_runs_without_master_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] from-stems runs where no master.plain.db exists [then] it plans and exits 0, [else stop]."""
    data_dir = tmp_path / "data"
    _state(data_dir, tmp_path)

    code = cli.main(["from-stems", "--data-dir", str(data_dir), "--dry-run"])

    assert code == 0
    assert "from-stems: 0 track(s)" in capsys.readouterr().out
