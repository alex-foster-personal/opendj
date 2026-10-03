"""Core D2 invariant: ``track_id`` is stable across Rekordbox + djay adapters.

A track with the same ISRC MUST produce byte-identical ``track_id`` values
on both the RB and djay export paths. This is what makes open-dj a
cross-vendor identifier.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.open_dj.adapters.djay import DjayTrackInput
from apps.open_dj.adapters.djay import build_library as dj_build
from apps.open_dj.adapters.rekordbox import (
    RBTrackInput,
)
from apps.open_dj.adapters.rekordbox import (
    build_library as rb_build,
)

# djay has no per-track source timestamp; these tests only compare
# track_id, but build_library() now requires an explicit modified_at
# whenever bpm/key/rating is set. None of these fixtures set those, so
# this constant is here purely to make that contract explicit if that
# ever changes.
_MODIFIED_AT = datetime(2026, 1, 5, 12, 30, 0, tzinfo=UTC)


@pytest.mark.requirement("OPEN-01")
def test_same_isrc_yields_same_track_id() -> None:
    """Strawman section 5 tier 1: normalised ISRC drives track_id."""
    rb = RBTrackInput(
        rb_id="rb1", title="Lanterns", artists=["Marlow Quay"],
        isrc="GBCEN0900132", duration_ms=634000,
        file_path="/a", size_bytes=1, mtime=1.0,
    )
    dj = DjayTrackInput(
        uuid="u1", title="Lanterns", artist="Marlow Quay",
        isrc="GBCEN0900132", file_path="/b",
        is_local=True, rating=None, duration_s=634.0,
        size_bytes=2, mtime=2.0,
    )
    rb_tid = rb_build([rb]).document["tracks"][0]["track_id"]
    dj_tid = dj_build([dj], modified_at=_MODIFIED_AT).document["tracks"][0]["track_id"]
    assert rb_tid == dj_tid


@pytest.mark.requirement("OPEN-01")
def test_different_isrc_yields_different_track_id() -> None:
    rb = RBTrackInput(
        rb_id="rb1", title="A", artists=["x"], isrc="GBCEN0900132",
        duration_ms=1000, file_path="/a", size_bytes=1, mtime=1.0,
    )
    dj = DjayTrackInput(
        uuid="u1", title="A", artist="x", isrc="USABC1234567",
        file_path="/b", is_local=True, rating=None, duration_s=1.0,
        size_bytes=1, mtime=1.0,
    )
    rb_tid = rb_build([rb]).document["tracks"][0]["track_id"]
    dj_tid = dj_build([dj], modified_at=_MODIFIED_AT).document["tracks"][0]["track_id"]
    assert rb_tid != dj_tid


@pytest.mark.requirement("OPEN-01")
def test_normalisation_equivalence() -> None:
    """Different ISRC formatting collapses to the same track_id."""
    rb = RBTrackInput(
        rb_id="rb1", title="A", artists=["x"], isrc="gb-cen-09-00132",
        duration_ms=1000, file_path="/a", size_bytes=1, mtime=1.0,
    )
    dj = DjayTrackInput(
        uuid="u1", title="A", artist="x", isrc="GBCEN0900132",
        file_path="/b", is_local=True, rating=None, duration_s=1.0,
        size_bytes=1, mtime=1.0,
    )
    rb_tid = rb_build([rb]).document["tracks"][0]["track_id"]
    dj_tid = dj_build([dj], modified_at=_MODIFIED_AT).document["tracks"][0]["track_id"]
    assert rb_tid == dj_tid


@pytest.mark.requirement("OPEN-01")
def test_fingerprint_tier_cross_adapter() -> None:
    """When both sides share fingerprint triple but no ISRC, tier 2 matches."""
    rb = RBTrackInput(
        rb_id="rb1", title="A", artists=["x"], isrc=None,
        duration_ms=1000, file_path="/a", size_bytes=100,
        mtime=1.0, fingerprint="A" * 80,
    )
    dj = DjayTrackInput(
        uuid="u1", title="A", artist="x", isrc=None,
        file_path="/b", is_local=True, rating=None, duration_s=1.0,
        size_bytes=100, mtime=999.0, fingerprint="A" * 80,
    )
    rb_tid = rb_build([rb]).document["tracks"][0]["track_id"]
    dj_tid = dj_build([dj], modified_at=_MODIFIED_AT).document["tracks"][0]["track_id"]
    assert rb_tid == dj_tid
