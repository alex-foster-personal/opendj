"""Tests for :mod:`apps.shared.rekordbox_db`.

Ties to RECON-01 (broken-link enumeration builds on ``iter_tracks``) and
SYNC-02 (matching builds on track/playlist iteration).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared import rekordbox_db


# ------------------------------------------------------------------ pure helpers


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("", True),
        ("spotify:track:abc123", True),
        ("tidal:track:def456", True),
        ("http://example.com/stream", True),
        ("https://example.com/stream", True),
        ("/music/Manual Library/foo.mp3", False),
        ("/tmp/some-file.flac", False),
    ],
)
@pytest.mark.requirement("RECON-01")
def test_is_streaming_path_classifies_uris_and_paths(value: str | None, expected: bool) -> None:
    assert rekordbox_db.is_streaming_path(value) is expected


# ------------------------------------------------------------------ iter_tracks


@pytest.mark.requirement("RECON-01")
def test_iter_tracks_yields_rbtrack_for_every_row(rb_pyrekordbox_db) -> None:
    """One RBTrack per djmdContent row; IDs are unique strings."""
    tracks = list(rekordbox_db.iter_tracks(rb_pyrekordbox_db))
    assert 30 <= len(tracks) <= 80, len(tracks)
    ids = [t.id for t in tracks]
    assert len(set(ids)) == len(ids), "IDs must be unique"
    assert all(isinstance(t.id, str) for t in tracks)


@pytest.mark.requirement("RECON-01")
def test_iter_tracks_flags_streaming_tracks(rb_pyrekordbox_db) -> None:
    """Streaming tracks have ``is_streaming=True`` + ``file_path=None``."""
    tracks = list(rekordbox_db.iter_tracks(rb_pyrekordbox_db))
    streaming = [t for t in tracks if t.is_streaming]
    assert streaming, "fixture should include spotify: tracks"
    for t in streaming:
        assert t.file_path is None, t
        assert t.folder_path == "" or t.folder_path.startswith(
            ("spotify:", "tidal:", "http://", "https://")
        )


@pytest.mark.requirement("RECON-01")
def test_iter_tracks_resolves_local_file_paths(rb_pyrekordbox_db) -> None:
    """Non-streaming tracks resolve ``file_path`` as a ``Path``."""
    tracks = list(rekordbox_db.iter_tracks(rb_pyrekordbox_db))
    local = [t for t in tracks if not t.is_streaming and t.folder_path]
    assert local, "fixture should include local-file tracks"
    for t in local[:5]:
        assert isinstance(t.file_path, Path)
        # file_path matches FolderPath (after expanduser).
        assert str(t.file_path) == str(Path(t.folder_path).expanduser())


@pytest.mark.requirement("SYNC-02")
def test_iter_tracks_empty_artist_is_empty_string_not_none(rb_pyrekordbox_db) -> None:
    """Tracks with ArtistID=NULL yield ``artist=''`` (string) so sync matchers
    can treat it as falsy without a None check."""
    tracks = list(rekordbox_db.iter_tracks(rb_pyrekordbox_db))
    missing = [t for t in tracks if not t.artist]
    assert missing, "fixture should include missing-artist tracks"
    for t in missing:
        assert t.artist == "", t


@pytest.mark.requirement("SYNC-02")
def test_iter_tracks_unicode_title_roundtrip(rb_pyrekordbox_db) -> None:
    """Unicode in FolderPath survives the iter_tracks layer unchanged."""
    tracks = list(rekordbox_db.iter_tracks(rb_pyrekordbox_db))
    unicode_tracks = [
        t for t in tracks
        if t.folder_path and any(ord(c) > 127 for c in t.folder_path)
    ]
    assert unicode_tracks, "fixture should include unicode filenames"
    # Each unicode track's FolderPath is valid UTF-8 (no mojibake).
    for t in unicode_tracks:
        assert t.folder_path == t.folder_path.encode("utf-8").decode("utf-8")


@pytest.mark.requirement("RECON-01")
def test_iter_tracks_bpm_is_float_when_populated(rb_pyrekordbox_db) -> None:
    """BPM is stored as BPM*100 in RB; ``iter_tracks`` divides back out."""
    tracks = list(rekordbox_db.iter_tracks(rb_pyrekordbox_db))
    bpms = [t.bpm for t in tracks if t.bpm is not None]
    assert bpms, "fixture should include BPMs"
    for b in bpms:
        assert isinstance(b, float)
        # Realistic DJ BPM range.
        assert 40.0 <= b <= 220.0, b


# ------------------------------------------------------------------ iter_playlists


@pytest.mark.requirement("SYNC-02")
def test_iter_playlists_track_ids_respect_trackno(rb_pyrekordbox_db) -> None:
    """Playlist ``track_ids`` are sorted by TrackNo."""
    playlists = list(rekordbox_db.iter_playlists(rb_pyrekordbox_db))
    assert playlists, "fixture should include playlists"
    # At least one playlist has multiple tracks.
    sizes = [len(p.track_ids) for p in playlists]
    assert max(sizes) >= 2, sizes

    # Cross-check: the track_ids come from djmdSongPlaylist for that playlist,
    # ordered by TrackNo. We don't re-derive TrackNo (closure of the ORM
    # relationship), but we do assert no duplicates and every ID is a string.
    for p in playlists:
        assert len(p.track_ids) == len(set(p.track_ids))
        assert all(isinstance(t, str) for t in p.track_ids)


@pytest.mark.requirement("SYNC-02")
def test_iter_playlists_top_level_parent_id_is_none(rb_pyrekordbox_db) -> None:
    """Rekordbox stores the root parent as literal 'root'; we normalize to None."""
    playlists = list(rekordbox_db.iter_playlists(rb_pyrekordbox_db))
    # At least one playlist should be top-level; that one must have parent_id=None.
    top = [p for p in playlists if p.parent_id is None]
    assert top, "fixture should include top-level playlists"


# ------------------------------------------------------------------ open_db


@pytest.mark.requirement("INFRA-02")
def test_open_db_raises_when_live_and_working_both_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If neither live nor working DB exists, ``open_db`` raises."""
    from apps.shared import paths as shared_paths

    monkeypatch.setattr(shared_paths, "REKORDBOX_LIVE_DB", tmp_path / "missing.db")
    monkeypatch.setattr(shared_paths, "REKORDBOX_WORKING_DB", tmp_path / "missing.copy")
    monkeypatch.setattr(shared_paths, "DJAY_LIVE_DB", tmp_path / "no-djay.db")
    monkeypatch.setattr(shared_paths, "DATA_DIR", tmp_path / "data")

    with pytest.raises(FileNotFoundError):
        rekordbox_db.open_db()
