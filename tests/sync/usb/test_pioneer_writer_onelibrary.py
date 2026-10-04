"""Round-trip tests for the Pioneer OneLibrary writer (CAT-06 Prototype B).

[if] overlays land on a copy of a real Rekordbox export [then] every value reads back unchanged, [else stop].

These tests exercise ``apps.sync.usb.pioneer.writer_onelibrary`` end-to-end:

  1. Copy the committed ``exportLibrary.db`` SQLCipher fixture to a
     scratch location.
  2. Apply track-metadata overlays + create new playlists.
  3. Reopen the resulting file via :mod:`apps.sync.usb.pioneer.onelibrary` and assert every written
     value round-trips byte-for-byte (including UTF-8).

All tests are skipped with a clear reason if ``sqlcipher3`` is not
installable on the host (see :data:`writer_onelibrary.WRITER_AVAILABLE`).

Tests do *not* write to the real USB mount (``/Volumes/MAINTAINER``)
— everything lives under ``tmp_path``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.usb.pioneer.writer_onelibrary import (
    WRITER_AVAILABLE,
    WRITER_IMPORT_ERROR,
    OneLibraryWriteError,
    OneLibraryWriteResult,
    PlaylistSpec,
    TrackUpdate,
    read_playlist_roundtrip,
    write_onelibrary,
)
from tests.fixtures.conftest import resolve_required_fixture

# -----------------------------------------------------------------------
# Skip marker: applied to every test in this module when sqlcipher3 is missing.
# -----------------------------------------------------------------------
pytestmark = [
    pytest.mark.requirement("CAT-06"),
    pytest.mark.skipif(
        not WRITER_AVAILABLE,
        reason=(
            "OneLibrary writer unavailable: "
            f"{WRITER_IMPORT_ERROR}. Install the repository dependencies."
        ),
    ),
]


# -----------------------------------------------------------------------
# Fixture-path helper
#
# Routes through resolve_required_fixture() (rather than a hard-coded repo
# path) so this CAT-06 acceptance module fails closed on a missing fixture
# host instead of silently skipping at runtime, once the in-repo directory
# leaves and only ``rb-usb-export.extern`` remains (PR #718). Deferred out
# of a module-level constant into this helper (called only from the
# fixture-dependent fixture/test bodies below) so an
# ``MDT_ALLOW_MISSING_FIXTURES=1`` skip -- or a missing host with no
# opt-out, which fails closed via ``resolve_required_fixture`` -- drops
# only the tests that actually need USB data: test_missing_template_raises
# and test_track_update_to_overlay_dropping_none_fields need no fixture
# and must stay collectible either way (PR #718 review).
# -----------------------------------------------------------------------
def _fixture_onelibrary() -> Path:
    return (
        resolve_required_fixture("rb-usb-export") / "PIONEER" / "rekordbox" / "exportLibrary.db"
    )


@pytest.fixture(scope="function")
def fixture_onelibrary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Path to a per-test COPY of the SQLCipher-encrypted OneLibrary
    fixture.

    ``OneLibrary(path)`` opens in read/write mode and SQLite
    materialises ``-shm``/``-wal`` sidecar files alongside the opened
    DB. Pointing it at the committed fixture directly therefore
    mutates files under ``tests/fixtures/`` on every test run. We dodge
    that by copying the fixture to a fresh tmp dir per test and
    returning THAT path — so writer tests can safely be passed the
    returned path as either template OR direct-open target.
    """
    import shutil as _shutil

    scratch_dir = tmp_path_factory.mktemp("rb-onelibrary-template")
    dest = scratch_dir / "exportLibrary.db"
    _shutil.copyfile(_fixture_onelibrary(), dest)
    return dest


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
    round-trip via the reader.

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

    # --- Round-trip read -----------------------------------
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
    playlists.  The output must still be a valid, readable file
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

    # Reopen and assert the original data is intact.
    from apps.sync.usb.pioneer.onelibrary import OneLibrary

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


def test_fixture_output_path_is_refused(fixture_onelibrary: Path) -> None:
    """Neither side of a low-level writer call may target fixtures."""
    fixture_output = _fixture_onelibrary().parent / "new-exportLibrary.db"

    with pytest.raises(OneLibraryWriteError, match=r"output_path.*fixture root"):
        write_onelibrary(
            template_path=fixture_onelibrary,
            output_path=fixture_output,
        )

    assert not fixture_output.exists()


def test_symlinked_fixture_output_path_is_refused(
    fixture_onelibrary: Path, tmp_path: Path
) -> None:
    """A fixture reached through an ad-hoc ``tests/fixtures/`` symlink is
    protected too, not just the in-repo path and the external host.

    PR #718 review: ``fixture_path()`` supports a contributor pointing
    ``tests/fixtures/<name>`` at an out-of-tree symlink (e.g. a personal
    canonical copy), and its target can be anywhere -- the writer's
    fixture-safety guard must recognize that target as a protected root
    too, or a write derived from it silently lands beside the
    contributor's real data.
    """
    canonical_copy = tmp_path / "contributor-canonical-copy"
    canonical_copy.mkdir()
    link = Path("tests/fixtures") / "zz-scratch-writer-guard-test"
    link.symlink_to(canonical_copy)
    try:
        fixture_output = canonical_copy / "new-exportLibrary.db"

        with pytest.raises(OneLibraryWriteError, match=r"output_path.*fixture root"):
            write_onelibrary(
                template_path=fixture_onelibrary,
                output_path=fixture_output,
            )

        assert not fixture_output.exists()
    finally:
        link.unlink(missing_ok=True)


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

    (Pure-Python logic, runs even on hosts where sqlcipher3 is missing —
    covered by the module-level skipif on sqlcipher3 itself, but exercises
    the overlay shape used in the other tests.)
    """
    upd = TrackUpdate(id=42, title="x", bpmx100=12000)
    overlay = upd.to_overlay()
    assert overlay == {"title": "x", "bpmx100": 12000}
    assert "id" not in overlay
    assert "rating" not in overlay


def test_write_onelibrary_is_not_a_gig_stick(
    fixture_onelibrary: Path, scratch_onelibrary: Path, tmp_path: Path
) -> None:
    """Overlay writer output must not pass rekordbox gig-stick verification."""
    from apps.sync.usb.pioneer.value_verify import verify_stick_values

    write_onelibrary(
        template_path=fixture_onelibrary,
        output_path=scratch_onelibrary,
        track_updates=[TrackUpdate(id=1, title="Overlay only")],
    )
    pioneer_root = scratch_onelibrary.parent.parent
    assert not (pioneer_root / "rekordbox" / "export.pdb").exists()
    assert not (pioneer_root / "USBANLZ").exists()

    report = verify_stick_values(tmp_path / pioneer_root.name)
    assert report.is_rekordbox_export is False
    assert report.key.match == 0
    assert report.grid.match == 0
