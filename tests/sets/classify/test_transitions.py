"""Tests for :mod:`apps.sets.transitions` (Plan 12-02 Step 1)."""
from __future__ import annotations

import pytest

from apps.sets.state import Event
from apps.sets.transitions import (
    FEATURE_COLUMNS,
    Transition,
    compute_features,
    find_transitions,
)


def _ev(ts: float, action: str, **kwargs) -> Event:
    return Event(
        session_id="s1",
        timestamp_s=ts,
        wall_clock="2026-04-17T21:30:00+00:00",
        action=action,
        source=kwargs.pop("source", "djay_monitor"),
        deck=kwargs.pop("deck", None),
        track_stable_id=kwargs.pop("track_stable_id", None),
        value=kwargs.pop("value", {}),
    )


def _fixture_timeline_three_transitions() -> list[Event]:
    """A->B blend, B->A cut, A->A quick-double (same-deck reload)."""
    return [
        _ev(0.0, "session_start", source="recorder"),
        _ev(5.0, "track_loaded", deck="A", track_stable_id="t1"),
        _ev(180.0, "track_loaded", deck="B", track_stable_id="t2"),
        _ev(200.0, "track_change", deck="B",
            value={"from_deck": "A", "to_deck": "B", "from_uuid": "t1", "to_uuid": "t2"}),
        _ev(208.0, "heartbeat", deck="A"),   # outgoing trail
        # cut (tiny overlap)
        _ev(360.0, "track_loaded", deck="A", track_stable_id="t3"),
        _ev(360.1, "track_change", deck="A",
            value={"from_deck": "B", "to_deck": "A", "from_uuid": "t2", "to_uuid": "t3"}),
        # quick-double on deck A
        _ev(500.0, "track_loaded", deck="A", track_stable_id="t4"),
        _ev(500.5, "track_change", deck="A",
            value={"from_deck": "A", "to_deck": "A", "from_uuid": "t3", "to_uuid": "t4"}),
        _ev(510.0, "session_end", source="recorder"),
    ]


@pytest.mark.requirement("SET-02")
def test_find_transitions_extracts_three_from_fixture():
    events = _fixture_timeline_three_transitions()
    transitions = find_transitions("s1", events=events)
    assert len(transitions) == 3
    assert [t.from_deck for t in transitions] == ["A", "B", "A"]
    assert [t.to_deck for t in transitions] == ["B", "A", "A"]


@pytest.mark.requirement("SET-02")
def test_find_transitions_computes_overlap_for_first():
    events = _fixture_timeline_three_transitions()
    first = find_transitions("s1", events=events)[0]
    # incoming_t = 180.0 (t2 loaded), outgoing_end = 208.0 (A heartbeat).
    assert first.features["overlap_s"] == pytest.approx(28.0)
    # fade = outgoing_end - t_change = 208.0 - 200.0 = 8.0
    assert first.features["fade_s"] == pytest.approx(8.0)
    assert first.features["incoming_preload_s"] == pytest.approx(20.0)
    assert first.features["is_same_deck_reload"] == 0.0


@pytest.mark.requirement("SET-02")
def test_find_transitions_marks_same_deck_reload():
    events = _fixture_timeline_three_transitions()
    third = find_transitions("s1", events=events)[2]
    assert third.from_deck == third.to_deck == "A"
    assert third.features["is_same_deck_reload"] == 1.0


@pytest.mark.requirement("SET-02")
def test_find_transitions_time_since_prev_transition():
    events = _fixture_timeline_three_transitions()
    ts = [t.t_change_s for t in find_transitions("s1", events=events)]
    # second transition is at 360.1; first at 200.0 -> diff = 160.1
    second_feats = find_transitions("s1", events=events)[1].features
    assert second_feats["time_since_prev_transition_s"] == pytest.approx(160.1)


@pytest.mark.requirement("SET-02")
def test_feature_columns_stable():
    """Regression: classifier depends on these exact names in order."""
    assert FEATURE_COLUMNS == (
        "overlap_s",
        "fade_s",
        "incoming_preload_s",
        "outgoing_trail_s",
        "time_since_prev_transition_s",
        "is_same_deck_reload",
    )


@pytest.mark.requirement("SET-02")
def test_find_transitions_requires_state_or_events():
    with pytest.raises(ValueError):
        find_transitions("s-missing")


@pytest.mark.requirement("SET-02")
def test_compute_features_handles_missing_outgoing():
    """Transition where no outgoing events exist still returns 0 overlap."""
    t = Transition(
        idx=0, t_start_s=0.0, t_change_s=10.0, t_end_s=10.0,
        from_deck="A", to_deck="B", from_track=None, to_track=None,
    )
    feats = compute_features(
        t,
        outgoing_load=None,
        incoming_load=None,
        outgoing_last=None,
        prev_change_ts=None,
    )
    assert feats["overlap_s"] == 0.0
    assert feats["fade_s"] == 0.0
    assert feats["time_since_prev_transition_s"] == 0.0
