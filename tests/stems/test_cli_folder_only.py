"""Stems for a library with no rekordbox: ``python -m apps.stems trickle`` and ``one``.

The analysis queue's stems step runs these two commands (``one`` for a track
order, ``trickle`` for a library refresh). Both used to load their tracks
through the rekordbox loader, which opens ``master.plain.db`` first, so on an
install with no rekordbox every stems request from the queue died with
``FileNotFoundError: MASTER_DB missing on disk`` and a folder-imported track
was never a candidate even where rekordbox existed.

[if] a library has no rekordbox [then] the stems commands select folder-imported tracks by the app's own stable id, [else stop].

Real state.db (the production schema and writer) and a real WAV stem bundle;
no worker is run and nothing is stubbed.

Regression one-liners:
  - if trickle raises on a missing master.plain.db instead of listing folder tracks then broken
  - if trickle lists a folder track that already has a bundle, or whose audio is gone, then broken
  - if `one` cannot find a folder-imported track by its stable id then broken
  - if `one` on an id that is no live track does not refuse loudly then broken
  - if a library with rekordbox loses its rekordbox tracks from trickle, or lists a track twice, then broken
  - if a rekordbox copy that is there but broken is treated as "no rekordbox" then broken
  - if an empty or corrupt master.plain.db passes as a healthy copy when no track maps to rekordbox then broken
"""
from __future__ import annotations

import json
import os
import sqlite3
import wave
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.stems.artifacts import STEM_PARTS
from apps.stems.cli import main

pytestmark = pytest.mark.requirement("STEM-63")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A folder-only library: three imported tracks, no rekordbox anywhere.

      todo     audio on disk, no bundle    -> the one trickle candidate
      bundled  audio on disk, valid bundle -> already done
      gone     audio path not on disk      -> not a candidate
    """
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    media = tmp_path / "media"
    media.mkdir()
    conn = state_db.open_rw(data / "state" / "state.db")
    writer = StateWriter(conn, actor="unit-test")
    for sid in ("todo", "bundled", "gone"):
        audio = media / f"{sid}.mp3"
        if sid != "gone":
            audio.write_bytes(b"audio " + sid.encode())
        writer.upsert_track(
            stable_id=sid,
            stable_id_tier="inferred",
            title=sid.title(),
            artists=["X"],
            album=None,
            isrc=None,
            duration_ms=4_000,
            file_path=str(audio),
        )
    conn.commit()
    conn.close()
    assert not (data / "master.plain.db").exists()

    bundle = data / "state" / "stems" / "bundled"
    bundle.mkdir(parents=True)
    for part in STEM_PARTS:
        with wave.open(str(bundle / f"{part}.wav"), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(44100)
            out.writeframes(b"\x00\x00" * 8)
    manifest = {
        "schema_version": 1,
        "stable_id": "bundled",
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/x.wav", "sha256": "a" * 64},
        "files": {part: f"{part}.wav" for part in STEM_PARTS},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return data


def test_trickle_selects_folder_tracks_with_no_rekordbox(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] there is no master.plain.db [then] trickle lists the folder tracks still missing stems, [else stop]."""
    assert main(["trickle", "--data-dir", str(data_dir), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "n=1/1" in out, out
    assert "] todo 'Todo'" in out, out
    # Controls: a bundled track and a track with no audio are never queued.
    assert "] bundled " not in out, out
    assert "] gone " not in out, out


def test_one_finds_a_folder_track_by_its_stable_id(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a folder-imported track is ordered by id [then] `one` resolves it, [else stop]."""
    assert main(["one", "--data-dir", str(data_dir), "--stable-id", "bundled", "--live"]) == 0
    assert "already have valid bundle" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="audio missing for gone"):
        main(["one", "--data-dir", str(data_dir), "--stable-id", "gone", "--live"])


def test_one_refuses_an_id_that_is_no_live_track(data_dir: Path) -> None:
    """[if] the id is no live track [then] `one` refuses by name, [else stop]."""
    with pytest.raises(SystemExit, match="unknown stable_id 'ghost'"):
        main(["one", "--data-dir", str(data_dir), "--stable-id", "ghost", "--live"])


def test_trickle_keeps_rekordbox_tracks_and_adds_folder_tracks_once(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] rekordbox holds some tracks and not others [then] trickle lists each track missing stems exactly once, [else stop]."""
    held_audio = data_dir.parent / "media" / "held.mp3"
    held_audio.write_bytes(b"audio held")
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    StateWriter(conn, actor="unit-test").upsert_track(
        stable_id="held",
        stable_id_tier="inferred",
        title="Held",
        artists=["X"],
        album=None,
        isrc=None,
        duration_ms=4_000,
        file_path=str(held_audio),
    )
    conn.execute(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, ?, ?)",
        ("held", "rekordbox", "v-held"),
    )
    conn.commit()
    conn.close()
    master = sqlite3.connect(data_dir / "master.plain.db")
    master.execute(
        "CREATE TABLE djmdContent (ID TEXT, Title TEXT, Length INTEGER, "
        "FolderPath TEXT, AnalysisDataPath TEXT, rb_local_deleted INTEGER)"
    )
    master.execute(
        "INSERT INTO djmdContent VALUES (?, ?, ?, ?, NULL, 0)",
        ("v-held", "Held In Rekordbox", 200, str(held_audio)),
    )
    master.commit()
    master.close()

    assert main(["trickle", "--data-dir", str(data_dir), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "n=2/2" in out, out
    assert out.count("] held 'Held In Rekordbox'") == 1, out
    assert out.count("] todo 'Todo'") == 1, out


def test_a_broken_rekordbox_copy_is_not_read_as_no_rekordbox(data_dir: Path) -> None:
    """[if] master.plain.db is there but is not a readable file [then] trickle fails loudly, [else stop]."""
    (data_dir / "master.plain.db").symlink_to(data_dir / "nowhere.db")

    with pytest.raises(FileNotFoundError, match="MASTER_DB missing on disk"):
        main(["trickle", "--data-dir", str(data_dir), "--dry-run"])


def _write_copy(path: Path, kind: str) -> None:
    if kind == "empty":
        path.write_bytes(b"")
    elif kind == "not-a-database":
        path.write_bytes(bytes(range(256)) * 32)
    elif kind == "no-rekordbox-tables":
        other = sqlite3.connect(path)
        other.execute("CREATE TABLE other (x)")
        other.commit()
        other.close()
    else:
        raise ValueError(kind)


@pytest.mark.parametrize("kind", ["empty", "not-a-database", "no-rekordbox-tables"])
@pytest.mark.parametrize("command", ["trickle", "one"])
def test_an_unreadable_rekordbox_copy_fails_even_when_no_track_maps_to_it(
    data_dir: Path, kind: str, command: str
) -> None:
    """[if] master.plain.db is a file but no rekordbox database, and no track maps to rekordbox [then] the stems commands fail loudly, [else stop].

    With no mapping no row is ever fetched from the copy, so only an explicit
    read of its schema tells a healthy copy from an empty or corrupt file.
    """
    _write_copy(data_dir / "master.plain.db", kind)
    argv = {
        "trickle": ["trickle", "--data-dir", str(data_dir), "--dry-run"],
        "one": ["one", "--data-dir", str(data_dir), "--stable-id", "bundled", "--live"],
    }[command]

    with pytest.raises(sqlite3.DatabaseError):
        main(argv)


def test_a_healthy_rekordbox_copy_that_maps_no_track_still_lists_folder_tracks(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the copy is a real rekordbox database and state maps no track to it [then] trickle lists the folder tracks, [else stop]."""
    master = sqlite3.connect(data_dir / "master.plain.db")
    master.execute("CREATE TABLE djmdContent (ID TEXT)")
    master.commit()
    master.close()

    assert main(["trickle", "--data-dir", str(data_dir), "--dry-run"]) == 0
    assert "] todo 'Todo'" in capsys.readouterr().out


def _mixed_pair(
    tmp_path: Path,
    *,
    folder_duration_ms: int | None,
    rekordbox_length_s: int,
    label: str = "data",
) -> Path:
    data = tmp_path / label
    (data / "state").mkdir(parents=True)
    media = data / "media"
    media.mkdir()
    folder_audio = media / "folder.mp3"
    folder_audio.write_bytes(b"audio folder")
    held_audio = media / "held.mp3"
    held_audio.write_bytes(b"audio held")

    conn = state_db.open_rw(data / "state" / "state.db")
    writer = StateWriter(conn, actor="unit-test")
    writer.upsert_track(
        stable_id="folder",
        stable_id_tier="inferred",
        title="Folder",
        artists=["X"],
        album=None,
        isrc=None,
        duration_ms=folder_duration_ms,
        file_path=str(folder_audio),
    )
    writer.upsert_track(
        stable_id="held",
        stable_id_tier="inferred",
        title="Held",
        artists=["X"],
        album=None,
        isrc=None,
        duration_ms=999_000,
        file_path=str(held_audio),
    )
    conn.execute(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, ?, ?)",
        ("held", "rekordbox", "v-held"),
    )
    conn.commit()
    conn.close()

    master = sqlite3.connect(data / "master.plain.db")
    master.execute(
        "CREATE TABLE djmdContent (ID TEXT, Title TEXT, Length INTEGER, "
        "FolderPath TEXT, AnalysisDataPath TEXT, rb_local_deleted INTEGER)"
    )
    master.execute(
        "INSERT INTO djmdContent VALUES (?, ?, ?, ?, NULL, 0)",
        ("v-held", "Held In Rekordbox", rekordbox_length_s, str(held_audio)),
    )
    master.commit()
    master.close()
    return data


@pytest.mark.requirement("STEM-9145")
def test_trickle_orders_mixed_library_by_stored_duration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] mixed library has equal rank [then] trickle --limit 1 picks shortest by stored duration, [else stop]."""
    data_dir = _mixed_pair(
        tmp_path, folder_duration_ms=180_000, rekordbox_length_s=300
    )
    assert (
        main(
            [
                "trickle",
                "--data-dir",
                str(data_dir),
                "--dry-run",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "n=1/2" in out, out
    assert "] folder 'Folder'" in out, out
    assert "] held " not in out.split("n=1/2")[1], out

    capsys.readouterr()
    data_dir = _mixed_pair(
        tmp_path,
        folder_duration_ms=300_000,
        rekordbox_length_s=180,
        label="data_swap",
    )
    assert (
        main(
            [
                "trickle",
                "--data-dir",
                str(data_dir),
                "--dry-run",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "n=1/2" in out, out
    assert "] held 'Held In Rekordbox'" in out, out


@pytest.mark.requirement("STEM-9145")
def test_trickle_sorts_folder_track_with_no_stored_length_last(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a folder track has no stored duration_ms [then] it sorts last and dry-run says why, [else stop]."""
    data_dir = _mixed_pair(
        tmp_path, folder_duration_ms=None, rekordbox_length_s=300
    )
    assert main(["trickle", "--data-dir", str(data_dir), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "n=2/2" in out, out
    held_pos = out.index("] held ")
    folder_pos = out.index("] folder ")
    assert held_pos < folder_pos, out
    assert "no stored length" in out, out
    assert "sorts last" in out, out


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a directory the user cannot enter")
def test_a_data_dir_the_user_cannot_enter_is_never_a_successful_run(data_dir: Path) -> None:
    """[if] the data dir cannot be entered [then] trickle raises instead of reporting an empty library, [else stop]."""
    data_dir.chmod(0)
    try:
        with pytest.raises(OSError):
            main(["trickle", "--data-dir", str(data_dir), "--dry-run"])
    finally:
        data_dir.chmod(0o755)
