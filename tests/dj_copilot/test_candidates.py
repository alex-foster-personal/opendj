"""Candidate filter tests (AI-01, AI-02)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apps.dj_copilot.candidates import filter_candidates
from apps.dj_copilot.session_context import PlayedTrack, SessionContext
from apps.shared.harmonic import TrackFeature

pytestmark = [
    pytest.mark.requirement("AI-01"),
    pytest.mark.requirement("AI-02"),
]


def _ctx(recent: list[PlayedTrack]) -> SessionContext:
    return SessionContext(
        recent=recent, source="manual", captured_at=datetime.now(timezone.utc)
    )


def _cur(**kw) -> PlayedTrack:
    defaults = {
        "stable_id": "cur",
        "artist": "Cur",
        "bpm": 120.0,
        "key_camelot": "8A",
        "energy": 5,
        "played_at": datetime.now(timezone.utc),
    }
    defaults.update(kw)
    return PlayedTrack(**defaults)


def test_empty_library_returns_empty() -> None:
    out, trace = filter_candidates(
        current_track=_cur(), library=[], context=_ctx([])
    )
    assert out == []
    assert trace.total_dropped() == 0


def test_bpm_window_drops_far_tracks() -> None:
    lib = [
        TrackFeature("t-a", "A", 121.0, "8A", 5),
        TrackFeature("t-b", "B", 150.0, "8A", 5),  # far
    ]
    out, trace = filter_candidates(current_track=_cur(), library=lib, context=_ctx([]))
    assert [c.stable_id for c in out] == ["t-a"]
    assert trace.dropped_bpm == 1


def test_artist_cooldown() -> None:
    lib = [
        TrackFeature("t-a", "Alpha", 121.0, "8A", 5),
        TrackFeature("t-b", "Beta", 121.0, "8A", 5),
    ]
    recent = [
        PlayedTrack(
            stable_id=f"r-{i}",
            artist="Alpha",
            bpm=120.0,
            key_camelot="8A",
            energy=5,
            played_at=datetime.now(timezone.utc),
        )
        for i in range(3)
    ]
    out, trace = filter_candidates(
        current_track=_cur(), library=lib, context=_ctx(recent)
    )
    assert [c.stable_id for c in out] == ["t-b"]
    assert trace.dropped_artist == 1


def test_current_self_dropped() -> None:
    lib = [
        TrackFeature("cur", "Cur", 120.0, "8A", 5),
        TrackFeature("t-a", "A", 120.0, "8A", 5),
    ]
    out, trace = filter_candidates(current_track=_cur(), library=lib, context=_ctx([]))
    assert "cur" not in {c.stable_id for c in out}
    assert trace.dropped_current == 1


def test_energy_slope_cap_rejects_reverse() -> None:
    """Rising recent slope + candidate well below current -> dropped."""
    recent = [
        PlayedTrack(
            stable_id=f"r-{i}",
            artist=f"R{i}",
            bpm=120.0,
            key_camelot="8A",
            energy=3 + i,
            played_at=datetime.now(timezone.utc),
        )
        for i in range(5)
    ]
    lib = [
        TrackFeature("t-ok", "Ok", 120.0, "8A", 7),
        TrackFeature("t-bad", "Bad", 120.0, "8A", 2),  # would reverse > 2 steps
    ]
    out, trace = filter_candidates(
        current_track=_cur(energy=7), library=lib, context=_ctx(recent)
    )
    ids = {c.stable_id for c in out}
    assert "t-ok" in ids
    assert "t-bad" not in ids
    assert trace.dropped_energy >= 1
