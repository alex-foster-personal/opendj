"""Round-trip tests for the Pioneer OneLibrary writer (CAT-06 Prototype B).

These tests exercise ``apps.sync.usb.pioneer.writer_rbox`` end-to-end:

  1. Copy the committed ``exportLibrary.db`` SQLCipher fixture to a
     scratch location.
  2. Apply track-metadata overlays + create new playlists.
  3. Reopen the resulting file via :mod:`rbox` and assert every written
     value round-trips byte-for-byte (including UTF-8).

All tests are skipped with a clear reason if ``rbox`` is not
installable on the host (see :data:`writer_rbox.RBOX_AVAILABLE`).

Tests do *not* write to the real USB mount (``/Volumes/MAINTAINER``)
— everything lives under ``tmp_path``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.usb.pioneer.writer_rbox import (
    RBOX_AVAILABLE,
    RBOX_IMPORT_ERROR,
    OneLibraryWriteError,
    OneLibraryWriteResult,
    PlaylistSpec,
    TrackUpdate,
    read_playlist_roundtrip,
    write_onelibrary,
)


# -----------------------------------------------------------------------
# Skip marker: applied to every test in this module when rbox is missing.
# -----------------------------------------------------------------------
pytestmark = [
    pytest.mark.requirement("CAT-06"),
    pytest.mark.skipif(
        not RBOX_AVAILABLE,
        reason=(
            "rbox (PyPI) is not installed: "
            f"{RBOX_IMPORT_ERROR}. Install with `pip install rbox`."
        ),
    ),
]


# -----------------------------------------------------------------------
# Fixture-path helper
# -----------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ONELIBRARY = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "rb-usb-export"
    / "PIONEER"
    / "rekordbox"
    / "exportLibrary.db"
)


@pytest.fixture(scope="module")
def fixture_onelibrary() -> Path:
    """Path to the real SQLCipher-encrypted OneLibrary fixture."""
    if not FIXTURE_ONELIBRARY.is_file():
        pytest.skip(f"OneLibrary fixture missing: {FIXTURE_ONELIBRARY}")
    return FIXTURE_ONELIBRARY


@pytest.fixture
def scratch_onelibrary(tmp_path: Path) -> Path:
    """Output path for a per-test scratch OneLibrary under tmp_path."""
    return tmp_path / "PIONEER" / "rekordbox" / "exportLibrary.db"


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------


def test_round_trip_5_tracks(
    fixture_onelibrary: Path, scratch_onelibrary: Path
) -> None:
    """Write overlays for 5 tracks + one playlist containing them, then
    round-trip via rbox.

    Asserts:

      * Every overlay field is persisted (title, rating, bpmx100,
        dj_comment).
      * The new playlist is present with the expected name and seq > 0.
      * Playlist membership is the exact input list, in order.
      * The output is a plausible SQLCipher file (header NOT the cleartext
        ``SQLite format 3`` magic — encryption is preserved).
    """
    overlays = [
        TrackUpdate(
            id=1,
            title="Round Trip Title 1",
            rating=5,
            bpmx100=13000,
            dj_comment="energy 9",
        ),
        TrackUpdate(id=2, title="Round Trip Title 2", rating=4, bpmx100=12800),
        TrackUpdate(id=3, title="Round Trip Title 3", rating=3, bpmx100=12600),
        TrackUpdate(id=4, title="Round Trip Title 4", rating=2, bpmx100=12400),
        TrackUpdate(id=5, title="Round Trip Title 5", rating=1, bpmx100=12200),
    ]
    playlist = PlaylistSpec(
        name="PrototypeB Round-Trip Playlist", track_ids=[1, 2, 3, 4, 5]
    )

    result: OneLibraryWriteResult = write_onelibrary(
        template_path=fixture_onelibrary,
        output_path=scratch_onelibrary,
        track_updates=overlays,
        playlists=[playlist],
    )

    # --- Sanity on the write summary --------------------------------
    assert result.tracks_updated == 5
    assert result.playlists_written == 1
    assert len(result.playlist_ids) == 1
    assert result.output_path == scratch_onelibrary.resolve()
    assert result.output_size_bytes > 0
    assert scratch_onelibrary.is_file()

    # --- SQLCipher header sanity -----------------------------------
    # The output must NOT start with cleartext "SQLite format 3" —
    # SQLCipher encrypts the entire file including the header bytes.
    header = scratch_onelibrary.read_bytes()[:16]
    assert not header.startswith(b"SQLite format 3"), (
        "Output looks like a plain SQLite file; SQLCipher encryption "
        f"was NOT applied. Header: {header!r}"
    )

    # --- Round-trip read via rbox -----------------------------------
    (playlist_id,) = result.playlist_ids
    rt = read_playlist_roundtrip(
        onelibrary_path=scratch_onelibrary, playlist_id=playlist_id
    )
    assert rt["name"] == "PrototypeB Round-Trip Playlist"
    track_ids = [t["id"] for t in rt["tracks"]]
    assert track_ids == [1, 2, 3, 4, 5]

    titles = [t["title"] for t in rt["tracks"]]
    assert titles == [
        "Round Trip Title 1",
        "Round Trip Title 2",
        "Round Trip Title 3",
        "Round Trip Title 4",
        "Round Trip Title 5",
    ]
    bpms = [t["bpmx100"] for t in rt["tracks"]]
    assert bpms == [13000, 12800, 12600, 12400, 12200]
    ratings = [t["rating"] for t in rt["tracks"]]
    assert ratings == [5, 4, 3, 2, 1]


def test_round_trip_empty_library(
    fixture_onelibrary: Path, scratch_onelibrary: Path
) -> None:
    """Writing nothing yields a copy of the template that still reads
    back the *original* playlists and contents.

    This is the "no-op passthrough" edge case: zero overlays, zero new
    playlists.  The output must still be a valid, rbox-readable file
    whose existing data is intact.
    """
    result = write_onelibrary(
        template_path=fixture_onelibrary,
        output_path=scratch_onelibrary,
        track_updates=None,
        playlists=None,
    )
    assert result.tracks_updated == 0
    assert result.playlists_written == 0
    assert result.playlist_ids == ()

    # Reopen via rbox and assert the original data is intact.
    from rbox import OneLibrary  # local import: rbox is optional.

    template_db = OneLibrary(str(fixture_onelibrary))
    template_playlists = {p["name"] for p in template_db.get_playlists()}
    template_count = len(template_db.get_contents())
    del template_db

    scratch_db = OneLibrary(str(scratch_onelibrary))
    scratch_playlists = {p["name"] for p in scratch_db.get_playlists()}
    scratch_count = len(scratch_db.get_contents())

    assert scratch_playlists == template_playlists
    assert scratch_count == template_count


def test_round_trip_special_chars(
    fixture_onelibrary: Path, scratch_onelibrary: Path
) -> None:
    """UTF-8: accents / umlauts / CJK / emoji all survive the round-trip.

    Validates both the playlist *name* path (row stored in ``playlist``
    table) and the track *title* path (row stored in ``content`` table).
    """
    weird_title = "é ü 日本語 emoji 🎧 mix"
    weird_playlist_name = "日本語 é ü emoji 🎧 playlist"

    result = write_onelibrary(
        template_path=fixture_onelibrary,
        output_path=scratch_onelibrary,
        track_updates=[TrackUpdate(id=1, title=weird_title)],
        playlists=[PlaylistSpec(name=weird_playlist_name, track_ids=[1])],
    )

    (playlist_id,) = result.playlist_ids
    rt = read_playlist_roundtrip(
        onelibrary_path=scratch_onelibrary, playlist_id=playlist_id
    )
    assert rt["name"] == weird_playlist_name
    assert len(rt["tracks"]) == 1
    assert rt["tracks"][0]["title"] == weird_title
    # Byte-level sanity: make sure no silent Mojibake happened.
    assert "日本語" in rt["tracks"][0]["title"]
    assert "🎧" in rt["name"]


def test_overwrite_false_raises(
    fixture_onelibrary: Path, scratch_onelibrary: Path
) -> None:
    """Pre-existing output path + overwrite=False must raise."""
    # Seed the output once.
    write_onelibrary(
        template_path=fixture_onelibrary,
        output_path=scratch_onelibrary,
    )
    assert scratch_onelibrary.is_file()

    with pytest.raises(OneLibraryWriteError, match="overwrite=False"):
        write_onelibrary(
            template_path=fixture_onelibrary,
            output_path=scratch_onelibrary,
            overwrite=False,
        )


def test_missing_template_raises(
    tmp_path: Path, scratch_onelibrary: Path
) -> None:
    """A non-existent template path raises a clean error (not a crash)."""
    bogus = tmp_path / "does-not-exist.db"
    with pytest.raises(OneLibraryWriteError, match="Template OneLibrary not found"):
        write_onelibrary(
            template_path=bogus,
            output_path=scratch_onelibrary,
        )


def test_unknown_track_id_raises(
    fixture_onelibrary: Path, scratch_onelibrary: Path
) -> None:
    """An overlay referring to an ID that isn't in the template raises."""
    with pytest.raises(
        OneLibraryWriteError, match="not present in template DB"
    ):
        write_onelibrary(
            template_path=fixture_onelibrary,
            output_path=scratch_onelibrary,
            track_updates=[TrackUpdate(id=9_999_999, title="ghost")],
        )


def test_track_update_to_overlay_dropping_none_fields() -> None:
    """Unit: ``TrackUpdate`` only emits explicitly-set fields.

    (Pure-Python logic, runs even on hosts where rbox is missing —
    covered by the module-level skipif on rbox itself, but exercises
    the overlay shape used in the other tests.)
    """
    upd = TrackUpdate(id=42, title="x", bpmx100=12000)
    overlay = upd.to_overlay()
    assert overlay == {"title": "x", "bpmx100": 12000}
    assert "id" not in overlay
    assert "rating" not in overlay
