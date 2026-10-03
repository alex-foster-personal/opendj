"""NATIVE-15 path-collision refusal during folder and rekordbox ingest.

Split from NATIVE-13 Thu 24 Sep 2026 (claude-review, PR #3829): NATIVE-13 moved
to v1.1 for an unrelated reason (the Windows parity gate has never run), but
this guard is a macOS (APFS, NFD filenames) data-loss guard already shipped in
v1, so it now has its own v1 id rather than moving out with NATIVE-13.

[if] ingest would create colliding paths [then] the guard refuses first, [else stop].
[if] ingest sees colliding paths for different recordings [then] import refuses, [else stop].
"""
from __future__ import annotations

import os
import sqlite3
import struct
import unicodedata
import wave
from pathlib import Path

import pytest

from apps.engine_core.setup import detect, importer
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder as folder_ingest
from apps.shared.state.ingest import rekordbox as rb_ingest
from apps.shared.state.ingest.path_collisions import (
    PathCollisionError,
    assert_no_path_collisions,
    is_streaming_path,
)
from apps.shared.state.writer import StateWriter
from tests.shared.state.test_ingest_rekordbox import _counts

pytestmark = pytest.mark.requirement("NATIVE-15")


def _write_wav(path: Path, seconds: float = 0.05) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(44100 * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return path


def _write_colliding_spellings(root: Path, name_a: str, name_b: str) -> tuple[Path, Path]:
    """Write two colliding-spelling files, or skip if this filesystem folds them into one.

    A case-insensitive and/or normalization-insensitive filesystem (default macOS
    APFS) can collapse two distinct spellings (e.g. "Song.wav"/"song.wav", NFC/NFD
    "café.wav") into a single directory entry before the code under test ever
    sees two paths, which would make the collision-refusal assertion vacuous. Skip
    explicitly with a clear reason rather than silently passing or failing; a
    case-sensitive filesystem (see test_same_physical_file_two_spellings_allowed's
    companion control) exercises the real check.
    """
    path_a = _write_wav(root / name_a)
    path_b = root / name_b
    if path_b.exists() and os.path.samefile(path_a, path_b):
        pytest.skip(
            f"filesystem folds {name_a!r} and {name_b!r} into one entry "
            "(case-insensitive and/or normalization-insensitive); "
            "needs a case-sensitive filesystem to exercise this collision"
        )
    path_b = _write_wav(path_b)
    return path_a, path_b


def _track_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]


def _event_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


@pytest.fixture
def writer(state_conn):
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="collision-test")
    try:
        yield writer
    finally:
        writer.close()


def test_nfc_nfd_collision_refuses_with_both_raw_paths(
    writer: StateWriter, state_conn, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    nfc_name = unicodedata.normalize("NFC", "caf\u00e9.wav")
    nfd_name = unicodedata.normalize("NFD", "caf\u00e9.wav")
    nfc_path, nfd_path = _write_colliding_spellings(root, nfc_name, nfd_name)

    with pytest.raises(PathCollisionError) as excinfo:
        folder_ingest.ingest_folder(writer, [root], dry_run=False)

    message = str(excinfo.value)
    assert str(nfc_path) in message
    assert str(nfd_path) in message
    assert _track_count(state_conn) == 0
    assert _event_count(state_conn) == 0


def test_case_only_collision_refuses_with_both_raw_paths(
    writer: StateWriter, state_conn, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    upper, lower = _write_colliding_spellings(root, "Song.wav", "song.wav")

    with pytest.raises(PathCollisionError) as excinfo:
        folder_ingest.ingest_folder(writer, [root], dry_run=False)

    message = str(excinfo.value)
    assert str(upper) in message
    assert str(lower) in message
    assert _track_count(state_conn) == 0
    assert _event_count(state_conn) == 0


def test_distinct_normalized_paths_import_both_tracks(
    writer: StateWriter, state_conn, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    _write_wav(root / "alpha.wav")
    _write_wav(root / "beta.wav")

    report = folder_ingest.ingest_folder(writer, [root], dry_run=False)

    assert report.tracks_inserted == 2
    assert report.tracks_skipped == 0
    assert _track_count(state_conn) == 2


def test_dry_run_collision_refuses_without_writing_state(
    writer: StateWriter, state_conn, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    _write_colliding_spellings(root, "Song.wav", "song.wav")

    with pytest.raises(PathCollisionError):
        folder_ingest.ingest_folder(writer, [root], dry_run=True)

    assert _track_count(state_conn) == 0
    assert _event_count(state_conn) == 0


def test_collision_checked_before_limit_slice(
    writer: StateWriter, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    _write_colliding_spellings(root, "Song.wav", "song.wav")
    _write_wav(root / "other.wav")

    with pytest.raises(PathCollisionError):
        folder_ingest.ingest_folder(writer, [root], dry_run=False, limit=1)


def test_same_physical_file_two_spellings_allowed(
    tmp_path: Path,
) -> None:
    """Case-insensitive hosts may only expose one spelling of a path."""
    root = tmp_path / "library"
    path = _write_wav(root / "Only.wav")
    alias = path.parent / "only.wav"
    # Compare inodes, not resolve(): on macOS APFS resolve() keeps the spelling
    # it was given, so a same-file case alias resolves to a different path.
    if not (alias.exists() and os.path.samefile(alias, path)):
        pytest.skip(
            "filesystem keeps case-only spellings distinct, so there is no "
            "second spelling of one file; needs a case-insensitive filesystem"
        )

    from apps.shared.state.ingest.path_collisions import assert_no_path_collisions

    assert_no_path_collisions([str(path), str(alias)])


def test_setup_folder_import_surfaces_collision(
    data_dir: Path, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    _write_colliding_spellings(root, "Song.wav", "song.wav")
    (data_dir / "state").mkdir(parents=True, exist_ok=True)

    def emit(_progress: float, _message: str) -> None:
        return None

    with pytest.raises(importer.SetupImportError) as excinfo:
        importer.run_folder_import(data_dir, emit=emit, roots=[root])

    assert excinfo.value.code == detect.CODE_INGEST_FAILED
    assert "Song.wav" in str(excinfo.value)
    assert "song.wav" in str(excinfo.value)
    assert not (data_dir / "setup.json").exists()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    return target


def test_streaming_paths_skip_collision_check() -> None:
    assert is_streaming_path("spotify:track:abc")
    assert_no_path_collisions(
        [
            "spotify:track:abc",
            "spotify:track:ABC",
            "https://example.com/song.mp3",
        ]
    )


def test_missing_files_with_colliding_keys_fail_closed(tmp_path: Path) -> None:
    missing_a = str(tmp_path / "Song.wav")
    missing_b = str(tmp_path / "song.wav")
    with pytest.raises(PathCollisionError) as excinfo:
        assert_no_path_collisions([missing_a, missing_b])
    assert missing_a in str(excinfo.value)
    assert missing_b in str(excinfo.value)


def _set_case_only_collision_paths(rb_db: Path, path_a: str, path_b: str) -> None:
    """Point two fixture tracks at case-colliding local paths via SQL."""
    conn = sqlite3.connect(rb_db)
    try:
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT ID FROM djmdContent "
                "WHERE COALESCE(rb_local_deleted, 0) = 0 "
                "LIMIT 2"
            ).fetchall()
        ]
        if len(ids) < 2:
            pytest.skip("fixture needs at least two local tracks")
        conn.execute(
            "UPDATE djmdContent SET FolderPath = ? WHERE ID = ?",
            (path_a, ids[0]),
        )
        conn.execute(
            "UPDATE djmdContent SET FolderPath = ? WHERE ID = ?",
            (path_b, ids[1]),
        )
        conn.commit()
    finally:
        conn.close()


def test_rekordbox_collision_refuses_without_partial_state(
    tmp_rb_db: Path, tmp_path: Path
) -> None:
    """Production Rekordbox6Database reader refuses before mutating seeded state."""
    from pyrekordbox import Rekordbox6Database

    path_a = str(tmp_path / "Song.wav")
    path_b = str(tmp_path / "song.wav")

    conn = state_db.open_rw(tmp_path / "state.db")
    bus = FakeEventBus()
    writer = StateWriter(conn, bus=bus, actor="rb-collision")
    try:
        rb_ingest.ingest_rb(writer, tmp_rb_db, dry_run=False)
        seed_counts = _counts(conn)
        assert seed_counts["tracks"] > 0
        assert seed_counts["events"] > 0
        before_counts = _counts(conn)
        before_events = list(bus.events)

        _set_case_only_collision_paths(tmp_rb_db, path_a, path_b)

        rb = Rekordbox6Database(path=str(tmp_rb_db), unlock=False)
        try:
            rows = list(rb_ingest._rb_rows(rb))
            folder_paths = [row["folder_path"] for row in rows if row["folder_path"]]
            assert path_a in folder_paths
            assert path_b in folder_paths
        finally:
            rb.close()

        # dry_run=False: live write path must not mutate already-persisted state (#3019)
        with pytest.raises(PathCollisionError) as excinfo:
            rb_ingest.ingest_rb(writer, tmp_rb_db, dry_run=False)

        message = str(excinfo.value)
        assert path_a in message
        assert path_b in message
        after_counts = _counts(conn)
        assert after_counts == before_counts, (
            f"collision refusal must not mutate state; "
            f"before={before_counts} after={after_counts}"
        )
        assert bus.events == before_events, (
            "collision refusal must not publish new bus events"
        )
    finally:
        writer.close()
        conn.close()
