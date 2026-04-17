"""Tests for :mod:`apps.open_dj.adapters.rekordbox`."""
from __future__ import annotations

import pytest

from apps.open_dj.adapters.rekordbox import (
    RBPlaylistInput,
    RBTrackInput,
    build_library,
)
from apps.open_dj.canon import to_canonical_bytes
from apps.open_dj.validate import validate_document


def _rb_track(**overrides) -> RBTrackInput:
    defaults = dict(
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
    )
    defaults.update(overrides)
    return RBTrackInput(**defaults)


@pytest.mark.requirement("OPEN-02")
class TestExport:
    def test_minimum_track_validates(self) -> None:
        result = build_library([_rb_track()])
        assert result.tracks_count == 1
        assert validate_document(result.document) == []

    def test_export_deterministic(self) -> None:
        """Same input -> byte-equal canonical output every call."""
        a = to_canonical_bytes(build_library([_rb_track()]).document)
        b = to_canonical_bytes(build_library([_rb_track()]).document)
        assert a == b

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
    def test_lowercase_isrc_uppercased(self) -> None:
        result = build_library([_rb_track(isrc="gbcen0900132")])
        t = result.document["tracks"][0]
        assert t["isrc"] == "GBCEN0900132"

    def test_hyphenated_isrc_stripped(self) -> None:
        result = build_library([_rb_track(isrc="GB-CEN-09-00132")])
        t = result.document["tracks"][0]
        assert t["isrc"] == "GBCEN0900132"
