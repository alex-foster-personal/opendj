"""Tests for :mod:`apps.open_dj.adapters.rekordbox`."""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from apps.open_dj.adapters.rekordbox import (
    RBPlaylistInput,
    RBTrackInput,
    build_library,
)
from apps.open_dj.canon import to_canonical_bytes
from apps.open_dj.validate import validate_document

# Fixed rekordbox-side "row last touched" timestamp. Deliberately not
# "now" anywhere in this file: the whole point of OPEN-02's provenance
# envelope is that it reports when ROW was last modified, not when the
# export happened to run.
_RB_UPDATED_AT = datetime(2026, 1, 5, 12, 30, 0, tzinfo=UTC)
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


def _rb_track(**overrides) -> RBTrackInput:
    defaults: dict[str, Any] = dict(
        rb_id="rb1",
        title="Strobe",
        artists=["deadmau5"],
        album="For Lack Of A Better Name",
        isrc="GBCEN0900132",
        duration_ms=634000,
        file_path="music/deadmau5/strobe.flac",
        size_bytes=50_000_000,
        mtime=1700000000.0,
        bpm=128.0,
        rating=5,
        updated_at=_RB_UPDATED_AT,
    )
    defaults.update(overrides)
    return RBTrackInput(**defaults)


@pytest.mark.requirement("OPEN-02")
class TestExport:
    """[if] the Rekordbox adapter exports data [then] its contract remains stable, [else stop]."""
    def test_minimum_track_validates(self) -> None:
        result = build_library([_rb_track()])
        assert result.tracks_count == 1
        assert validate_document(result.document) == []

    def test_export_deterministic(self) -> None:
        """Same input -> byte-equal canonical output every call."""
        a = to_canonical_bytes(build_library([_rb_track()]).document)
        b = to_canonical_bytes(build_library([_rb_track()]).document)
        assert a == b

    def test_export_never_touches_wall_clock(self) -> None:
        """Regression for PR #4119 fast-tier CI flake.

        Two exports of the same untouched library disagreed on bytes because
        ``bpm``/``rating`` ``modified_at`` was stamped from ``datetime.now()``
        rather than the rekordbox row's own ``updated_at``. Run two real
        exports that straddle a wall-clock second, prove they agree byte for
        byte, and prove every modified_at in the document is the row's own.
        """
        a = to_canonical_bytes(build_library([_rb_track()]).document)
        time.sleep(_CROSS_A_WALL_CLOCK_SECOND_S)
        document = build_library([_rb_track()]).document
        assert a == to_canonical_bytes(document)
        assert _all_modified_at(document) == {"2026-01-05T12:30:00Z"}

    def test_export_raises_when_provenance_field_missing_source_timestamp(
        self,
    ) -> None:
        """Fail fast: a track with bpm/key/rating but no updated_at must
        error loudly rather than have the export silently substitute the
        wall clock for the missing rekordbox-side timestamp."""
        with pytest.raises(RuntimeError, match="updated_at"):
            build_library([_rb_track(updated_at=None)], include_cues=False)

    def test_bpm_and_rating_modified_at_matches_source_row(self) -> None:
        """Control: modified_at carries the INTENDED value (the source
        row's updated_at), not merely *some* timestamp."""
        result = build_library([_rb_track()])
        track = result.document["tracks"][0]
        assert track["bpm"]["modified_at"] == "2026-01-05T12:30:00Z"
        assert track["rating"]["modified_at"] == "2026-01-05T12:30:00Z"

    def test_track_id_matches_stable_id(self) -> None:
        """D2: exported track_id == apps.shared.state.ids.stable_id for the same ISRC."""
        from apps.shared.state.ids import stable_id
        result = build_library([_rb_track()])
        tid = result.document["tracks"][0]["track_id"]
        expected, tier = stable_id(isrc="GBCEN0900132")
        assert tier == "isrc"
        assert tid == expected

    def test_playlists_reference_exported_track_ids(self) -> None:
        tracks = [
            _rb_track(rb_id="a", isrc="GBCEN0900132"),
            _rb_track(rb_id="b", isrc="USABC1234567", title="Other"),
        ]
        playlists = [
            RBPlaylistInput(rb_id="p1", name="Setlist",
                            track_rb_ids=["a", "b"])
        ]
        result = build_library(tracks, playlists)
        pl = result.document["playlists"][0]
        track_ids = [t["track_id"] for t in result.document["tracks"]]
        assert pl["tracks_ordered"] == track_ids
        assert pl["playlist_id"] == "rb_pl_p1"

    def test_cue_points_included_when_flag_set(self) -> None:
        track = _rb_track(cue_points=[
            {"position_ms": 0, "type": "memory", "name": "intro"},
            {"position_ms": 32000, "type": "hot", "slot": 0},
        ])
        result = build_library([track])
        assert result.cue_points_count == 2
        assert len(result.document["tracks"][0]["cue_points"]) == 2

    def test_cues_excluded_when_include_cues_false(self) -> None:
        track = _rb_track(cue_points=[{"position_ms": 0, "type": "memory"}])
        result = build_library([track], include_cues=False)
        assert result.cue_points_count == 0
        assert "cue_points" not in result.document["tracks"][0]

    def test_tier3_track_gets_x_marker(self) -> None:
        """Tracks without ISRC/fingerprint get tier 3 + x_track_id_tier marker."""
        track = _rb_track(isrc=None, file_path="/x/y.mp3", mtime=123.0,
                          size_bytes=None)
        result = build_library([track])
        assert result.document["tracks"][0]["x_track_id_tier"] == "inferred"

    def test_synthetic_content_hash_gets_marker(self) -> None:
        """Without pre-computed content_hash, adapter derives a synthetic one."""
        track = _rb_track()
        result = build_library([track])
        t = result.document["tracks"][0]
        assert t["x_content_hash_mode"] == "inferred"
        assert t["content_hash"].startswith("sha256:")
        assert len(t["content_hash"]) == len("sha256:") + 64

    def test_real_content_hash_no_marker(self) -> None:
        track = _rb_track(content_hash_hex="a" * 64)
        result = build_library([track])
        t = result.document["tracks"][0]
        assert t["content_hash"] == "sha256:" + ("a" * 64)
        assert "x_content_hash_mode" not in t

    def test_bpm_wrapped_as_provenance(self) -> None:
        result = build_library([_rb_track()])
        bpm = result.document["tracks"][0]["bpm"]
        assert bpm["value"] == 128.0
        assert bpm["source"] == "rekordbox"
        assert "modified_at" in bpm

    def test_rating_wrapped_as_provenance(self) -> None:
        result = build_library([_rb_track()])
        rating = result.document["tracks"][0]["rating"]
        assert rating["value"] == 5
        assert rating["source"] == "rekordbox"


@pytest.mark.requirement("OPEN-02")
class TestIsrcNormalisation:
    """[if] ISRC values are normalized [then] their canonical form is preserved, [else stop]."""
    def test_lowercase_isrc_uppercased(self) -> None:
        result = build_library([_rb_track(isrc="gbcen0900132")])
        t = result.document["tracks"][0]
        assert t["isrc"] == "GBCEN0900132"

    def test_hyphenated_isrc_stripped(self) -> None:
        result = build_library([_rb_track(isrc="GB-CEN-09-00132")])
        t = result.document["tracks"][0]
        assert t["isrc"] == "GBCEN0900132"
