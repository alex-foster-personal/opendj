"""Pins the two streaming-path predicates and every caller that reads them.

T3b decomposition map D1: ``is_streaming_path`` existed four times with three
different prefix sets and two different answers for the empty string. This
module is the contract that keeps them unified. It asserts, per call site,
which of the two questions that site is actually asking:

  * :func:`platform_paths.is_streaming_uri`   -- "is this a streaming URI?"
    Empty/None -> ``False``. Feeds wire fields that claim a track streams.
  * :func:`platform_paths.is_unplayable_path` -- "is there no local file?"
    Empty/None -> ``True``. Feeds every "skip it, nothing on disk" branch.

If a future refactor collapses these into one predicate, exactly one of the
two blocks below fails, naming the data it would have corrupted.
"""
from __future__ import annotations

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox import paths as rb_paths
from apps.shared import platform_paths, rekordbox_db
from apps.webui import crate_sync

# The union prefix set. Before T3b, rb_vendor/crate_sync knew "soundcloud:"
# but not "http(s)://", and rekordbox_db knew "http(s)://" but not
# "soundcloud:". Both omissions were bugs, so the union is canonical.
_STREAMING_URIS: tuple[str, ...] = (
    "spotify:track:abc123",
    "tidal:track:def456",
    "soundcloud:track:ghi789",
    "http://example.com/stream",
    "https://example.com/stream",
)
_LOCAL_PATHS: tuple[str, ...] = (
    "/music/Manual Library/foo.mp3",
    "/tmp/some-file.flac",
    "/Users/dev/Music/track.aiff",
)


# ----- the two canonical predicates ---------------------------------------


@pytest.mark.parametrize("uri", _STREAMING_URIS)
@pytest.mark.requirement("RECON-01")
def test_streaming_uris_are_classified_by_both_predicates(uri: str) -> None:
    assert platform_paths.is_streaming_uri(uri) is True
    assert platform_paths.is_unplayable_path(uri) is True


@pytest.mark.parametrize("path", _LOCAL_PATHS)
@pytest.mark.requirement("RECON-01")
def test_local_paths_are_classified_by_neither_predicate(path: str) -> None:
    assert platform_paths.is_streaming_uri(path) is False
    assert platform_paths.is_unplayable_path(path) is False


@pytest.mark.parametrize("empty", [None, ""])
@pytest.mark.requirement("RECON-01")
def test_empty_is_unplayable_but_not_streaming(empty: str | None) -> None:
    """The whole reason there are two predicates, not one.

    An absent path names no local file, so ``is_unplayable_path`` is True.
    It is also not a streaming URI, so ``is_streaming_uri`` is False. Merge
    them and one of the two callers below starts lying.
    """
    assert platform_paths.is_unplayable_path(empty) is True
    assert platform_paths.is_streaming_uri(empty) is False


@pytest.mark.requirement("RECON-01")
def test_one_prefix_set_shared_by_every_call_site() -> None:
    """No module keeps a private copy of the prefix tuple."""
    assert rb_config.STREAMING_PREFIXES is platform_paths.STREAMING_PREFIXES
    assert crate_sync.STREAMING_PREFIXES is platform_paths.STREAMING_PREFIXES
    assert set(platform_paths.STREAMING_PREFIXES) == {
        "tidal:",
        "soundcloud:",
        "spotify:",
        "http://",
        "https://",
    }


# ----- caller expectations, one block per question ------------------------


@pytest.mark.parametrize("empty", [None, ""])
@pytest.mark.requirement("RECON-01")
def test_rb_vendor_reports_pathless_tracks_as_not_streaming(empty: str | None) -> None:
    """``build_track_rows`` puts this straight on the wire as ``is_streaming``.

    A rekordbox row with no FolderPath is pathless, not a Spotify row. If
    this flips to True the browser renders missing local tracks with
    streaming styling, and ``routes/reconcile.py``'s documented contract
    ("``is_streaming`` always false" for broken rows) breaks.
    """
    assert rb_paths.is_streaming_path(empty) is False
    assert crate_sync._is_streaming(empty) is False


@pytest.mark.parametrize("empty", [None, ""])
@pytest.mark.requirement("RECON-01")
def test_rekordbox_db_reports_pathless_tracks_as_unplayable(empty: str | None) -> None:
    """``iter_tracks`` sets ``file_path=None`` off this answer.

    If it flips to False, ``RBTrack.file_path`` becomes ``Path("")`` --
    which is ``Path(".")``, an existing directory -- so a pathless track
    reads as a present local file and ``list_broken`` starts emitting ".".
    """
    assert rekordbox_db.is_streaming_path(empty) is True


@pytest.mark.parametrize("uri", _STREAMING_URIS)
@pytest.mark.requirement("RECON-01")
def test_reconcile_relocate_union_is_now_a_single_answer(uri: str) -> None:
    """``reconcile._is_local`` / ``relocate._is_local`` union both predicates.

    They did that because neither prefix set was complete on its own. With
    one union set the two calls agree on every URI, which is the evidence
    that the union -- not either original tuple -- was always the right set.
    """
    assert rekordbox_db.is_streaming_path(uri) is True
    assert rb_paths.is_streaming_path(uri) is True
