"""NATIVE-13 path-collision refusal during folder and rekordbox ingest.

[if] ingest would create colliding paths [then] assert_no_path_collisions refuses before state is written, [else stop].
[if] ingest sees colliding paths for different recordings [then] import refuses with PathCollisionError, [else stop].
"""
from __future__ import annotations

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

pytestmark = pytest.mark.requirement("NATIVE-13")


def _write_wav(path: Path, seconds: float = 0.05) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(44100 * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return path


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
    nfc_path = _write_wav(root / nfc_name)
    nfd_path = _write_wav(root / nfd_name)

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
    upper = _write_wav(root / "Song.wav")
    lower = _write_wav(root / "song.wav")

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
    _write_wav(root / "Song.wav")
    _write_wav(root / "song.wav")

    with pytest.raises(PathCollisionError):
        folder_ingest.ingest_folder(writer, [root], dry_run=True)

    assert _track_count(state_conn) == 0
    assert _event_count(state_conn) == 0


def test_collision_checked_before_limit_slice(
    writer: StateWriter, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    _write_wav(root / "Song.wav")
    _write_wav(root / "song.wav")
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
    if alias.exists() and alias.resolve() != path.resolve():
        pytest.skip(
            "platform can create two distinct case-only files; "
            "same-inode test needs a case-insensitive filesystem"
        )
    if not alias.exists():
        alias = path

    from apps.shared.state.ingest.path_collisions import assert_no_path_collisions

    assert_no_path_collisions([str(path), str(alias)])


def test_setup_folder_import_surfaces_collision(
    data_dir: Path, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    _write_wav(root / "Song.wav")
    _write_wav(root / "song.wav")
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


class _FakeRbDb:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_rb_row(rb_id: str, folder_path: str) -> dict:
    return {
        "id": rb_id,
        "title": "collision-track",
        "artist": "artist",
        "album": "album",
        "folder_path": folder_path,
        "is_streaming": False,
        "isrc": None,
        "bpm": None,
        "rating": None,
        "duration_ms": 180_000,
        "file_size": 4242,
        "key_name": None,
        "updated_at": None,
    }


def _patch_rb_ingest(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict],
    fake_db: _FakeRbDb,
) -> None:
    import pyrekordbox

    monkeypatch.setattr(
        pyrekordbox,
        "Rekordbox6Database",
        lambda *args, **kwargs: fake_db,
        raising=False,
    )
    monkeypatch.setattr(rb_ingest, "_rb_rows", lambda _rb: iter(rows))
    monkeypatch.setattr(rb_ingest, "_rb_playlists", lambda _rb: iter(()))


def test_rekordbox_collision_refuses_without_partial_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    conn = state_db.open_rw(tmp_path / "state.db")
    bus = FakeEventBus()
    writer = StateWriter(conn, bus=bus, actor="rb-collision")
    fake_db = _FakeRbDb()
    path_a = str(tmp_path / "Song.wav")
    path_b = str(tmp_path / "song.wav")
    rows = [_make_rb_row("R1", path_a), _make_rb_row("R2", path_b)]
    _patch_rb_ingest(monkeypatch, rows, fake_db)

    try:
        with pytest.raises(PathCollisionError) as excinfo:
            rb_ingest.ingest_rb(writer, tmp_path / "rb.db", dry_run=True)

        message = str(excinfo.value)
        assert path_a in message
        assert path_b in message
        assert writer.bus is bus
        assert fake_db.closed is True
        assert _track_count(conn) == 0
        assert _event_count(conn) == 0
        assert bus.events == []
    finally:
        writer.close()
        conn.close()
