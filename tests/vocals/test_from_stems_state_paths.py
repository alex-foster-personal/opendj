"""from-stems reads track audio from state.db, never from master.plain.db.

The packaged app has no decrypted rekordbox copy, so a from-stems run that
opened ``master.plain.db`` failed every library refresh with
``MASTER_DB missing on disk``.

[if] there is no master.plain.db [then] from-stems still finds each live track's audio, [else stop].

A folder-imported track has no rekordbox mapping at all; from-stems keys on
the app's own stable id, so it is covered exactly like a mapped one
(VOCALS-03).

Regression one-liners:
  - if from-stems needs master.plain.db then broken
  - if a live track's state.db audio path is not what from-stems sees then broken
  - if a track with no rekordbox mapping is left out of from-stems then broken
  - if a deleted track enters from-stems then broken
  - if a data dir's path-map.json is ignored then a remapped library skips every track
"""

from __future__ import annotations

from pathlib import Path

import json
import wave

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.stems.artifacts import STEM_PARTS
from apps.vocals import cli

pytestmark = pytest.mark.requirement("PARITY-08")


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
    """[if] there is no master.plain.db [then] live tracks, mapped or not, still resolve to their audio, [else stop]."""
    data_dir = tmp_path / "data"
    audio = _state(data_dir, tmp_path)
    assert not (data_dir / "master.plain.db").exists()

    tracks = cli.load_state_tracks(cli.Ctx(data_dir=data_dir))

    assert [t.stable_id for t in tracks] == ["mapped", "unmapped"]
    assert [t.audio_path for t in tracks] == [audio["mapped"], audio["unmapped"]]
    assert all(t.audio_on_disk for t in tracks)


@pytest.mark.requirement("VOCALS-03")
def test_state_tracks_can_be_narrowed_to_requested_ids(tmp_path: Path) -> None:
    """[if] ids are given [then] only those live tracks load, a deleted or unknown id loads nothing, [else stop]."""
    data_dir = tmp_path / "data"
    audio = _state(data_dir, tmp_path)

    tracks = cli.load_state_tracks(
        cli.Ctx(data_dir=data_dir), ["unmapped", "deleted", "ghost"]
    )

    assert [t.stable_id for t in tracks] == ["unmapped"]
    assert tracks[0].audio_path == audio["unmapped"]


def test_state_tracks_apply_the_data_dirs_path_map(tmp_path: Path) -> None:
    """[if] state.db stores a path only path-map.json resolves [then] the track still finds its audio, [else stop]."""
    data_dir = tmp_path / "data"
    audio = _state(data_dir, tmp_path)
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    for table in ("tracks", "track_locations"):
        conn.execute(
            f"UPDATE {table} SET file_path = ? WHERE stable_id = 'mapped'",
            ("/Volumes/Gone/mapped.wav",),
        )
    conn.commit()
    conn.close()
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": "/Volumes/Gone", "to": str(tmp_path)}]})
    )

    tracks = cli.load_state_tracks(cli.Ctx(data_dir=data_dir), ["mapped"])

    assert tracks[0].audio_path == audio["mapped"]


def _write_bundle(stems: Path, stable_id: str) -> None:
    """A real, loadable v1 bundle: aligned silent PCM16 WAV parts plus manifest."""
    bundle = stems / stable_id
    bundle.mkdir(parents=True)
    for part in STEM_PARTS:
        with wave.open(str(bundle / f"{part}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 4410)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stable_id": stable_id,
                "model": {"name": "htdemucs", "version": "4.0.1"},
                "source": {"path": "/tmp/source.wav", "sha256": "a" * 64},
                "files": {p: f"{p}.wav" for p in STEM_PARTS},
            }
        ),
        encoding="utf-8",
    )


def test_from_stems_runs_without_master_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] from-stems runs where no master.plain.db exists [then] it plans every live bundle, mapped or not, and exits 0, [else stop]."""
    data_dir = tmp_path / "data"
    _state(data_dir, tmp_path)
    stems = data_dir / "state" / "stems"
    for sid in ("deleted", "mapped", "unmapped"):
        _write_bundle(stems, sid)

    code = cli.main(["from-stems", "--data-dir", str(data_dir), "--dry-run"])

    captured = capsys.readouterr()
    assert code == 0
    assert "from-stems: 2 track(s)" in captured.out
    assert "1. mapped 'Mapped'" in captured.out
    assert "2. unmapped 'Unmapped'" in captured.out
    assert "[SKIP unknown-track] deleted" in captured.err
