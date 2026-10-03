"""Tests for :mod:`apps.open_dj.adapters.djay`."""
from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from apps.open_dj.adapters.djay import (
    DjayPlaylistInput,
    DjayTrackInput,
    build_library,
)
from apps.open_dj.canon import to_canonical_bytes
from apps.open_dj.validate import validate_document

# djay exposes no per-track source timestamp (unlike rekordbox's
# DjmdContent.updated_at), so build_library() takes an explicit
# modified_at clock parameter instead. Tests pass a fixed value so
# determinism does not depend on when the test happens to run.
_MODIFIED_AT = datetime(2026, 1, 5, 12, 30, 0, tzinfo=UTC)
# modified_at is stamped to the second, so a longer gap puts the two exports in
# different wall-clock seconds: a leaked now() would then change the bytes.
_CROSS_A_WALL_CLOCK_SECOND_S = 1.1


def _all_modified_at(node: object) -> set[str]:
    """Every provenance ``modified_at`` anywhere in an exported document."""
    if isinstance(node, dict):
        found = {node["modified_at"]} if "modified_at" in node else set()
        for value in node.values():
            found |= _all_modified_at(value)
        return found
    if isinstance(node, list):
        return set().union(*(_all_modified_at(item) for item in node))
    return set()


def _dj_track(**overrides) -> DjayTrackInput:
    defaults = dict(
        uuid="abc-123",
        title="Lanterns",
        artist="Marlow Quay",
        isrc="GBCEN0900132",
        file_path="/music/Marlow Quay/lanterns.flac",
        is_local=True,
        rating=5,
        duration_s=634.0,
        size_bytes=50_000_000,
        mtime=1700000000.0,
        bpm=128.0,
    )
    defaults.update(overrides)
    return DjayTrackInput(**defaults)


@pytest.mark.requirement("OPEN-02")
class TestExport:
    """[if] the djay adapter exports data [then] its output contract remains stable, [else stop]."""
    def test_minimum_track_validates(self) -> None:
        result = build_library([_dj_track()], modified_at=_MODIFIED_AT)
        assert result.tracks_count == 1
        assert validate_document(result.document) == []

    def test_duration_seconds_to_ms(self) -> None:
        result = build_library(
            [_dj_track(duration_s=60.5)], modified_at=_MODIFIED_AT
        )
        t = result.document["tracks"][0]
        assert t["duration_ms"] == 60500

    def test_streaming_track_flagged(self) -> None:
        track = _dj_track(
            uuid="streaming-1",
            isrc=None,
            file_path="spotify:track:xyz",
            is_local=False,
            rating=None,
            duration_s=None,
        )
        result = build_library([track], modified_at=_MODIFIED_AT)
        t = result.document["tracks"][0]
        assert t["x_djay_streaming"] is True

    def test_track_id_matches_stable_id(self) -> None:
        from apps.shared.state.ids import stable_id
        result = build_library([_dj_track()], modified_at=_MODIFIED_AT)
        tid = result.document["tracks"][0]["track_id"]
        expected, _ = stable_id(isrc="GBCEN0900132")
        assert tid == expected

    def test_multi_artist_string_split(self) -> None:
        """djay stores artists as one comma-separated string."""
        result = build_library(
            [_dj_track(artist="Artist A, Artist B, Artist C")],
            modified_at=_MODIFIED_AT,
        )
        t = result.document["tracks"][0]
        assert t["artists"] == ["Artist A", "Artist B", "Artist C"]

    def test_rating_zero_omitted(self) -> None:
        """djay rating=0 is the unset sentinel; do not emit as provenance."""
        result = build_library([_dj_track(rating=0)], modified_at=_MODIFIED_AT)
        t = result.document["tracks"][0]
        assert "rating" not in t

    def test_rating_present_wrapped(self) -> None:
        result = build_library([_dj_track(rating=4)], modified_at=_MODIFIED_AT)
        rating = result.document["tracks"][0]["rating"]
        assert rating["value"] == 4
        assert rating["source"] == "djay"

    def test_vendor_id_includes_uuid(self) -> None:
        result = build_library([_dj_track(uuid="uuid-42")], modified_at=_MODIFIED_AT)
        vendors = result.document["tracks"][0]["vendor_ids"]
        assert vendors == {"djay": "uuid-42"}

    def test_playlist_references_track_ids(self) -> None:
        tracks = [
            _dj_track(uuid="u1", isrc="GBCEN0900132"),
            _dj_track(uuid="u2", isrc="USABC1234567", title="Other"),
        ]
        playlists = [DjayPlaylistInput(uuid="p1", name="Set",
                                       track_uuids=["u1", "u2"])]
        result = build_library(tracks, playlists, modified_at=_MODIFIED_AT)
        pl = result.document["playlists"][0]
        track_ids = [t["track_id"] for t in result.document["tracks"]]
        assert pl["tracks_ordered"] == track_ids

    def test_deterministic_export(self) -> None:
        a = to_canonical_bytes(
            build_library([_dj_track()], modified_at=_MODIFIED_AT).document
        )
        b = to_canonical_bytes(
            build_library([_dj_track()], modified_at=_MODIFIED_AT).document
        )
        assert a == b

    def test_export_never_touches_wall_clock(self) -> None:
        """Same class of PR #4119 regression as the rekordbox adapter: two real
        exports straddling a wall-clock second agree byte for byte, and every
        modified_at in the document is the supplied clock, never now()."""
        a = to_canonical_bytes(
            build_library([_dj_track()], modified_at=_MODIFIED_AT).document
        )
        time.sleep(_CROSS_A_WALL_CLOCK_SECOND_S)
        document = build_library([_dj_track()], modified_at=_MODIFIED_AT).document
        assert a == to_canonical_bytes(document)
        assert _all_modified_at(document) == {"2026-01-05T12:30:00Z"}

    def test_export_raises_when_provenance_field_missing_modified_at(self) -> None:
        """Fail fast: bpm/key/rating set but no modified_at supplied must
        error loudly, not silently fall back to the wall clock."""
        with pytest.raises(RuntimeError, match="modified_at"):
            build_library([_dj_track()])

    def test_bpm_and_rating_modified_at_matches_supplied_clock(self) -> None:
        """Control: modified_at carries the INTENDED value (the explicit
        clock passed in), not merely *some* timestamp."""
        result = build_library([_dj_track()], modified_at=_MODIFIED_AT)
        track = result.document["tracks"][0]
        assert track["bpm"]["modified_at"] == "2026-01-05T12:30:00Z"
        assert track["rating"]["modified_at"] == "2026-01-05T12:30:00Z"
