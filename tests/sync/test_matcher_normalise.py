"""Tests for ``apps.sync.matcher._normalise_for_match`` (SYNC-02).

Ported from companion ``~/Desktop/music-dj/tests/test_sync.py`` but trimmed
to the normalisation-only surface area (the SyncEngine/writer tests are
out of scope for Phase 2).
"""
from __future__ import annotations

import pytest

from apps.sync.matcher import (
    UnifiedTrack,
    _make_match_key,
    _normalise_for_match,
)


pytestmark = pytest.mark.requirement("SYNC-02")


class TestNormaliseBasics:
    def test_empty_returns_empty(self) -> None:
        assert _normalise_for_match("") == ""

    def test_whitespace_collapse(self) -> None:
        assert _normalise_for_match("  Hello   World  ") == "hello world"

    def test_lowercase(self) -> None:
        assert _normalise_for_match("LOUD NOISES") == "loud noises"

    def test_nfc_normalised(self) -> None:
        # Pre-composed vs decomposed \u00e9 (e + combining acute).
        pre = "caf\u00e9"
        dec = "cafe\u0301"
        assert _normalise_for_match(pre) == _normalise_for_match(dec)


class TestFeaturingVariants:
    def test_featuring_becomes_ft(self) -> None:
        assert _normalise_for_match("Song featuring Artist") == "song ft. artist"

    def test_feat_dot_becomes_ft(self) -> None:
        assert _normalise_for_match("Song feat. Artist") == "song ft. artist"

    def test_feat_no_dot_becomes_ft(self) -> None:
        assert _normalise_for_match("Song feat Artist") == "song ft. artist"


class TestRemixStripping:
    def test_parenthesised_remix_stripped(self) -> None:
        assert (
            _normalise_for_match("Good Song (Extended Mix)") == "good song"
        )

    def test_bracketed_edit_stripped(self) -> None:
        assert (
            _normalise_for_match("Good Song [Club Edit]") == "good song"
        )

    def test_non_remix_parens_preserved(self) -> None:
        # "(2021)" does not match remix/edit/version/mix/radio/extended/...
        assert "2021" in _normalise_for_match("Good Song (2021)")

    def test_dub_version_stripped(self) -> None:
        assert _normalise_for_match("Track (Dub Version)") == "track"


class TestMatchKey:
    def test_stable_key_across_variants(self) -> None:
        k1 = _make_match_key("Hello (Extended Mix)", "Artist One")
        k2 = _make_match_key("hello", "artist one")
        assert k1 == k2

    def test_keys_differ_for_different_tracks(self) -> None:
        k1 = _make_match_key("Song A", "Artist 1")
        k2 = _make_match_key("Song B", "Artist 1")
        assert k1 != k2


class TestUnifiedTrackEquality:
    def test_eq_compares_on_normalised_title_artist(self) -> None:
        a = UnifiedTrack(title="Song (Club Mix)", artist="Artist")
        b = UnifiedTrack(title="song", artist="artist")
        assert a == b

    def test_hash_matches_eq(self) -> None:
        a = UnifiedTrack(title="Song (Club Mix)", artist="Artist")
        b = UnifiedTrack(title="song", artist="artist")
        assert hash(a) == hash(b)

    def test_different_tracks_not_equal(self) -> None:
        a = UnifiedTrack(title="Song A", artist="Artist")
        b = UnifiedTrack(title="Song B", artist="Artist")
        assert a != b
