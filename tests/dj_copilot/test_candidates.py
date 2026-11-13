"""Candidate filter tests (AI-01, AI-02)."""

from __future__ import annotations

import pytest

from apps.dj_copilot.candidates import filter_candidates, recording_identity, similar_library_candidates
from apps.dj_copilot.session_context import PlayedTrack, SessionContext
from apps.shared.harmonic import TrackFeature
from tests.dj_copilot.conftest import FIXED_NOW

pytestmark = [
    pytest.mark.requirement("AI-01"),
    pytest.mark.requirement("AI-02"),
]


def _ctx(recent: list[PlayedTrack]) -> SessionContext:
    return SessionContext(recent=recent, source="manual", captured_at=FIXED_NOW)


def _cur(**kw) -> PlayedTrack:
    defaults = {
        "stable_id": "cur",
        "artist": "Cur",
        "bpm": 120.0,
        "key_camelot": "8A",
        "energy": 5,
        "played_at": FIXED_NOW,
    }
    defaults.update(kw)
    return PlayedTrack(**defaults)


def test_empty_library_returns_empty() -> None:
    out, trace = filter_candidates(current_track=_cur(), library=[], context=_ctx([]))
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
            played_at=FIXED_NOW,
        )
        for i in range(3)
    ]
    out, trace = filter_candidates(current_track=_cur(), library=lib, context=_ctx(recent))
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
            played_at=FIXED_NOW,
        )
        for i in range(5)
    ]
    lib = [
        TrackFeature("t-ok", "Ok", 120.0, "8A", 7),
        TrackFeature("t-bad", "Bad", 120.0, "8A", 2),  # would reverse > 2 steps
    ]
    out, trace = filter_candidates(current_track=_cur(energy=7), library=lib, context=_ctx(recent))
    ids = {c.stable_id for c in out}
    assert "t-ok" in ids
    assert "t-bad" not in ids
    assert trace.dropped_energy >= 1


@pytest.mark.requirement("INTEL-12031")
def test_similar_library_candidates_drop_reference_only() -> None:
    """[if] Specific filters the library [then] the reference track is excluded, [else stop]."""
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    lib = [cur, TrackFeature("n1", "B", 121.0, "8A", 5)]
    out = similar_library_candidates(current=cur, library=lib)
    assert [t.stable_id for t in out] == ["n1"]


@pytest.mark.requirement("SETPREP-12113")
def test_session_stable_id_dropped_with_null_artist() -> None:
    """[if] stable_id is in session tail [then ⛔️] dropped_played counts it, [else stop]."""
    recent = [
        PlayedTrack(
            stable_id="played-once",
            artist=None,
            bpm=120.0,
            key_camelot="8A",
            energy=5,
            played_at=FIXED_NOW,
        )
    ]
    lib = [TrackFeature("played-once", None, 121.0, "8A", 5)]
    out, trace = filter_candidates(current_track=_cur(), library=lib, context=_ctx(recent))
    assert out == []
    assert trace.dropped_played == 1
    assert trace.dropped_notes["played-once"] == ["played: played-once"]
    assert trace.total_dropped() == 1


@pytest.mark.requirement("SETPREP-12113")
def test_played_copy_outside_bpm_window_counts_as_played() -> None:
    """[if] session play copy is far in BPM [then ⛔️] dropped_played not dropped_bpm, [else stop]."""
    recent = [
        PlayedTrack(
            stable_id="p1",
            artist="A",
            bpm=120.0,
            key_camelot="8A",
            energy=5,
            played_at=FIXED_NOW,
            title="Song",
        )
    ]
    lib = [TrackFeature("p1", "A", 200.0, "8A", 5, title="Song")]
    out, trace = filter_candidates(current_track=_cur(), library=lib, context=_ctx(recent))
    assert out == []
    assert trace.dropped_played == 1
    assert trace.dropped_bpm == 0


@pytest.mark.requirement("SETPREP-12113")
def test_recording_identity_drop_not_artist_cooldown() -> None:
    """[if] candidate matches played recording [then ⛔️] note names matched play, [else stop]."""
    recent = [
        PlayedTrack(
            stable_id="p1",
            artist="Artist feat. Guest",
            bpm=120.0,
            key_camelot="8A",
            energy=5,
            played_at=FIXED_NOW,
            title="6 - Song Name",
        ),
        *[
            PlayedTrack(
                stable_id=f"pad-{i}",
                artist=f"Other{i}",
                bpm=120.0,
                key_camelot="8A",
                energy=5,
                played_at=FIXED_NOW,
            )
            for i in range(8)
        ],
    ]
    lib = [TrackFeature("c1", "ARTIST", 121.0, "8A", 5, title="Song  name")]
    out, trace = filter_candidates(current_track=_cur(), library=lib, context=_ctx(recent))
    assert out == []
    assert trace.dropped_played == 1
    assert trace.dropped_notes["c1"] == ["same recording as played: p1"]


@pytest.mark.requirement("SETPREP-12113")
def test_remix_not_same_recording() -> None:
    """[if] candidate is a remix title [then ⛔️] it stays a candidate, [else stop]."""
    recent = [
        PlayedTrack(
            stable_id="p1",
            artist=None,
            bpm=120.0,
            key_camelot="8A",
            energy=5,
            played_at=FIXED_NOW,
            title="Song Name",
        )
    ]
    lib = [TrackFeature("c1", "Artist", 121.0, "8A", 5, title="Song Name (Remix)")]
    out, trace = filter_candidates(current_track=_cur(), library=lib, context=_ctx(recent))
    assert len(out) == 1
    assert trace.dropped_played == 0


@pytest.mark.requirement("SETPREP-12113")
@pytest.mark.parametrize(
    ("title", "artist", "expected"),
    [
        ("9A - 6 - Song", "A & B", "a|song"),
        ("  ", "A", None),
        (None, "A", None),
        ("Song", None, "|song"),
        ("Song", "A x B", "a|song"),
    ],
)
def test_recording_identity_parity(title: str | None, artist: str | None, expected: str | None) -> None:
    """[if] title/artist pairs match PLAY-16 [then ⛔️] identity matches TS, [else stop]."""
    assert recording_identity(title, artist) == expected
