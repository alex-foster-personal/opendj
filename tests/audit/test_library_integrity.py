"""Tests for the library-integrity guard (apps.audit.library_integrity).

Deterministic unit tests exercise the pure core with an injected
``exists`` predicate + ``home`` — they never touch the real DB and always
run in CI. One gated ``live_library`` test runs the same invariant against
the real Rekordbox working copy; it is the fool-proof regression guard the
DJ runs locally / before a sync. It is skipped unless MDT_LIVE_LIBRARY=1
so CI on a machine without the library stays green.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from apps.audit.library_integrity import (
    DEFAULT_THRESHOLD,
    IntegrityReport,
    LibraryIntegrityError,
    assert_healthy,
    check_integrity,
    rehome_path,
)


@dataclass
class FakeTrack:
    folder_path: str
    is_streaming: bool = False


def _exists_from(paths: set[str]):
    return lambda p: p in paths


# ----- rehome_path --------------------------------------------------------


def test_rehome_swaps_user_prefix():
    assert rehome_path("/Users/dev/Music/x.mp3", home="/Users/dev") == (
        "/Users/dev/Music/x.mp3"
    )


def test_rehome_tolerates_rekordbox_double_slash():
    assert rehome_path("//Users/dev/Music/x.mp3", home="/Users/dev") == (
        "/Users/dev/Music/x.mp3"
    )


def test_rehome_returns_none_without_home_prefix():
    assert rehome_path("/Volumes/SLATER/x.mp3", home="/Users/dev") is None
    assert rehome_path("", home="/Users/dev") is None


# ----- check_integrity classification ------------------------------------


def test_present_when_file_exists():
    rep = check_integrity(
        [FakeTrack("/Users/dev/Music/a.mp3")],
        exists=_exists_from({"/Users/dev/Music/a.mp3"}),
        home="/Users/dev",
    )
    assert (rep.present, rep.rehomable, rep.missing) == (1, 0, 0)


def test_rehomable_when_only_home_prefix_differs():
    # File lives under the *current* home; DB still points at the old user.
    rep = check_integrity(
        [FakeTrack("//Users/dev/Music/a.mp3")],
        exists=_exists_from({"/Users/dev/Music/a.mp3"}),
        home="/Users/dev",
    )
    assert (rep.present, rep.rehomable, rep.missing) == (0, 1, 0)
    assert rep.broken == 1  # rehomable still counts as broken-until-fixed


def test_missing_when_nowhere():
    rep = check_integrity(
        [FakeTrack("//Users/dev/Music/gone.mp3")],
        exists=_exists_from(set()),
        home="/Users/dev",
    )
    assert (rep.present, rep.rehomable, rep.missing) == (0, 0, 1)
    assert rep.missing_examples == ["//Users/dev/Music/gone.mp3"]
    assert rep.stale_home_prefixes == {"//Users/dev": 1}


def test_streaming_and_empty_paths_skipped():
    rep = check_integrity(
        [
            FakeTrack("spotify:track:abc", is_streaming=True),
            FakeTrack(""),
            FakeTrack("/Users/dev/Music/a.mp3"),
        ],
        exists=_exists_from({"/Users/dev/Music/a.mp3"}),
        home="/Users/dev",
    )
    assert rep.total == 3
    assert rep.streaming == 1
    assert rep.with_path == 1  # empty-path track excluded from denominator
    assert rep.present == 1


def test_ratios_use_with_path_denominator():
    rep = check_integrity(
        [
            FakeTrack("/Users/dev/Music/a.mp3"),       # present
            FakeTrack("//Users/old/Music/b.mp3"),       # rehomable
            FakeTrack("//Users/old/Music/c.mp3"),       # missing
            FakeTrack("//Users/old/Music/d.mp3"),       # missing
        ],
        exists=_exists_from({"/Users/dev/Music/a.mp3", "/Users/dev/Music/b.mp3"}),
        home="/Users/dev",
    )
    assert rep.with_path == 4
    assert rep.missing == 2 and rep.rehomable == 1
    assert rep.broken_ratio == pytest.approx(0.75)
    assert rep.missing_ratio == pytest.approx(0.5)


# ----- assert_healthy invariant ------------------------------------------


def test_assert_healthy_noop_when_all_present():
    rep = check_integrity(
        [FakeTrack("/Users/dev/Music/a.mp3")],
        exists=_exists_from({"/Users/dev/Music/a.mp3"}),
        home="/Users/dev",
    )
    assert_healthy(rep)  # must not raise


def test_assert_healthy_noop_on_empty_library():
    assert_healthy(IntegrityReport())  # nothing to assert; no raise


def test_assert_healthy_raises_over_threshold_with_actionable_message():
    rep = check_integrity(
        [FakeTrack("//Users/dev/Music/x.mp3") for _ in range(100)],
        exists=_exists_from(set()),
        home="/Users/dev",
    )
    with pytest.raises(LibraryIntegrityError) as exc:
        assert_healthy(rep, threshold=DEFAULT_THRESHOLD)
    msg = str(exc.value)
    assert "/Users/dev" in msg  # names the stale prefix
    assert "100/100" in msg


@pytest.mark.parametrize("bad", [-0.1, 1.5, 10.0])
def test_assert_healthy_rejects_out_of_range_threshold(bad):
    rep = check_integrity(
        [FakeTrack("/Users/dev/Music/a.mp3")],
        exists=_exists_from({"/Users/dev/Music/a.mp3"}),
        home="/Users/dev",
    )
    with pytest.raises(ValueError, match="threshold"):
        assert_healthy(rep, threshold=bad)


@pytest.mark.parametrize("ok", [0.0, 0.02, 1.0])
def test_assert_healthy_accepts_boundary_thresholds(ok):
    assert_healthy(IntegrityReport(), threshold=ok)  # empty report: no raise


# ----- live regression guard (gated) -------------------------------------


@pytest.mark.skipif(
    os.environ.get("MDT_LIVE_LIBRARY") != "1",
    reason="set MDT_LIVE_LIBRARY=1 to run the live Rekordbox integrity guard",
)
def test_live_library_resolves_on_disk():
    """FAILS loudly when the real library has broken track locations.

    This is the test that catches the "most tracks weren't in expected
    location" failure. Run: MDT_LIVE_LIBRARY=1 pytest tests/audit -k live
    """
    from apps.shared import rekordbox_db

    db = rekordbox_db.open_db()
    try:
        rep = check_integrity(rekordbox_db.iter_tracks(db))
    finally:
        db.close()
    assert_healthy(rep, threshold=DEFAULT_THRESHOLD)
