"""Regressions for review round ten on PR #1549.

[if] any round-ten review finding on PR #1549 recurs [then] fail, [else stop].

Fourth file in this series only because the third would otherwise mix
unrelated lane concerns; same discipline as the other three -- the claim was
RUN and the wrong behavior observed before anything changed, and each fix
goes red under a mutation back to its pre-fix source.

-Claude
"""
from __future__ import annotations

import pytest

from apps.analysis import store as analysis_store
from apps.analysis.lanes import (
    LaneContractError,
    LaneResult,
    validate_lane_payload,
    validate_lane_result,
)
from tests.analysis_contract.conftest import beatgrid_payload, key_payload, own_record

#-----------------------------------------------------------------------------
# P2 round 10: a tempo-change marker must be a valid point on the timeline
#-----------------------------------------------------------------------------

def test_a_negative_tempo_change_at_s_is_refused() -> None:
    """Before the track starts is not a valid marker position."""
    payload = beatgrid_payload(tempo_changes=1)
    payload["tempo_changes"][0]["at_s"] = -0.5
    with pytest.raises(LaneContractError, match="at_s"):
        validate_lane_payload("beatgrid", payload)


def test_decreasing_tempo_change_markers_are_refused() -> None:
    """Two markers out of chronological order cannot describe one timeline."""
    payload = beatgrid_payload(tempo_changes=2)
    payload["tempo_changes"][0]["at_s"] = 150.0
    payload["tempo_changes"][1]["at_s"] = 100.0
    with pytest.raises(LaneContractError, match="strictly increase"):
        validate_lane_payload("beatgrid", payload)


def test_equal_tempo_change_markers_are_refused() -> None:
    """Two markers at the same instant are not chronologically ordered either."""
    payload = beatgrid_payload(tempo_changes=2)
    payload["tempo_changes"][0]["at_s"] = 120.0
    payload["tempo_changes"][1]["at_s"] = 120.0
    with pytest.raises(LaneContractError, match="strictly increase"):
        validate_lane_payload("beatgrid", payload)


def test_a_beatgrid_extending_past_the_records_duration_is_refused(db) -> None:
    """`duration_s` is on the record, not the lane payload, so this is checked

    at the write boundary (`beats_within_duration`, threaded into
    `validate_lane_result`), not by `validate_lane_payload` alone -- there is
    nothing to compare against without the record. Once a valid grid can
    never outlive `duration_s` (this check's own job), a tempo-change marker
    beyond `duration_s` is always also beyond the grid's own last beat, so
    `_validate_beatgrid`'s payload-level check alone would already refuse it
    -- this exercises the grid itself outliving the record, which is the
    scenario the payload-level check cannot see.
    """
    payload = beatgrid_payload(grid_span_s=310.0)  # own_record's duration_s is 300.0
    with pytest.raises(LaneContractError, match="duration_s"):
        analysis_store.upsert_record(
            own_record(lane="beatgrid", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )


def test_tempo_changes_within_duration_rejects_a_marker_past_duration_directly() -> None:
    """Direct unit coverage of the write-boundary function's own logic.

    See `beats_within_duration`'s docstring: once a grid is bounded to
    `duration_s`, a marker past `duration_s` is also past the grid's own
    last beat and never reaches this function through the full write path
    (`_validate_beatgrid` refuses it first) -- so its own branch is
    exercised directly here instead.
    """
    from apps.analysis.lane_payloads import tempo_changes_within_duration

    payload = {"tempo_changes": [{"at_s": 301.0}]}
    with pytest.raises(LaneContractError, match="duration_s"):
        tempo_changes_within_duration(payload, 300.0)


def test_a_tempo_change_at_or_before_duration_is_accepted(db) -> None:
    """Positive control: the boundary itself, and comfortably inside it, both write."""
    payload = beatgrid_payload(tempo_changes=1)
    payload["tempo_changes"][0]["at_s"] = 300.0  # exactly own_record's duration_s
    analysis_store.upsert_record(
        own_record(lane="beatgrid", result=LaneResult(status="ok", payload=payload)),
        conn=db,
    )


def test_valid_ordered_tempo_changes_are_still_accepted() -> None:
    """Positive control: chronologically ordered, in-bounds markers still pass."""
    validate_lane_payload("beatgrid", beatgrid_payload(tempo_changes=3))


#-----------------------------------------------------------------------------
# P2 round 10: a key segment must be a valid bar-indexed range
#-----------------------------------------------------------------------------

def test_a_fractional_bar_index_is_refused() -> None:
    payload = key_payload()
    payload["segments"]["segments"][0]["start_bar"] = 0.5
    with pytest.raises(LaneContractError, match="integer"):
        validate_lane_payload("key", payload)


def test_a_bool_bar_index_is_refused() -> None:
    """`bool` is an `int` subclass in Python; the shape check must not be fooled."""
    payload = key_payload()
    payload["segments"]["segments"][0]["end_bar"] = True
    with pytest.raises(LaneContractError, match="integer"):
        validate_lane_payload("key", payload)


def test_a_reversed_bar_range_is_refused() -> None:
    payload = key_payload(segments=1)
    payload["segments"]["segments"][0]["start_bar"] = 16
    payload["segments"]["segments"][0]["end_bar"] = 8
    with pytest.raises(LaneContractError, match="start_bar"):
        validate_lane_payload("key", payload)


def test_a_zero_length_bar_range_is_refused() -> None:
    payload = key_payload(segments=1)
    payload["segments"]["segments"][0]["start_bar"] = 8
    payload["segments"]["segments"][0]["end_bar"] = 8
    with pytest.raises(LaneContractError, match="start_bar"):
        validate_lane_payload("key", payload)


def test_a_reversed_time_range_within_a_segment_is_refused() -> None:
    payload = key_payload(segments=1)
    payload["segments"]["segments"][0]["start_s"] = 30.0
    payload["segments"]["segments"][0]["end_s"] = 10.0
    with pytest.raises(LaneContractError, match="start_s"):
        validate_lane_payload("key", payload)


def test_overlapping_segments_in_bar_order_are_refused() -> None:
    """The second segment starts before the first one ends: an overlap."""
    payload = key_payload(segments=2)
    segs = payload["segments"]["segments"]
    assert segs[0]["end_bar"] == segs[1]["start_bar"] == 16
    segs[1]["start_bar"] = 8  # now overlaps segs[0]'s 0..16
    with pytest.raises(LaneContractError, match="contiguous"):
        validate_lane_payload("key", payload)


def test_a_gap_between_segments_in_bar_order_is_refused() -> None:
    """Non-overlapping is not enough on its own: a gap is also not contiguous."""
    payload = key_payload(segments=2)
    segs = payload["segments"]["segments"]
    segs[1]["start_bar"] = 24  # leaves bars 16..24 uncovered
    with pytest.raises(LaneContractError, match="contiguous"):
        validate_lane_payload("key", payload)


def test_segments_overlapping_in_time_are_refused() -> None:
    """Bar order can be contiguous while the timestamps still overlap."""
    payload = key_payload(segments=2)
    segs = payload["segments"]["segments"]
    segs[1]["start_s"] = segs[0]["start_s"]  # starts before segment 0 even ends
    with pytest.raises(LaneContractError, match="start_s"):
        validate_lane_payload("key", payload)


def test_valid_ordered_key_segments_are_still_accepted() -> None:
    """Positive control: the multi-segment fixture other tests mutate is itself valid."""
    validate_lane_payload("key", key_payload(segments=4))


def test_key_change_count_is_derived_only_from_a_validated_segments_block(db) -> None:
    """The write boundary rejects the bad block before any count is ever derived.

    `_key_change_count` in `canonical.py` reads `result.payload["segments"]`
    from a row the store already holds -- so the control that matters is that
    an invalid block never reaches the store at all, not a check inside the
    projection.
    """
    payload = key_payload(segments=2)
    payload["segments"]["segments"][1]["start_bar"] = 8  # overlap, as above
    with pytest.raises(LaneContractError, match="contiguous"):
        analysis_store.upsert_record(
            own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )
    (count,) = db.execute("SELECT COUNT(*) FROM analysis").fetchone()
    assert count == 0, "the invalid segments block must never reach the store"


#-----------------------------------------------------------------------------
# P2 review on PR #1562: a tempo-change marker needs a beat to land on
#-----------------------------------------------------------------------------

def test_a_tempo_change_past_the_grids_last_beat_is_refused() -> None:
    """A change after the last beat has no beats left to place it on."""
    payload = beatgrid_payload(tempo_changes=1)
    last_beat_t = payload["beats"][-1]["t"]
    payload["tempo_changes"][0]["at_s"] = last_beat_t + 5.0
    with pytest.raises(LaneContractError, match="last beat"):
        validate_lane_payload("beatgrid", payload)


def test_a_tempo_change_at_the_grids_last_beat_is_accepted() -> None:
    """Positive control: the grid's own last beat is a real beat to place it on."""
    payload = beatgrid_payload(tempo_changes=0)
    last_beat_t = payload["beats"][-1]["t"]
    payload["tempo_changes"] = [
        {"at_s": last_beat_t, "bpm_before": 128.0, "bpm_after": 132.0, "confidence": 0.8}
    ]
    validate_lane_payload("beatgrid", payload)


def test_a_tempo_change_before_the_grids_first_beat_is_refused() -> None:
    """Leading silence: the grid's first beat can start after t=0.0.

    A marker before that first beat has no beat under it either -- the same
    reason a marker past the LAST beat is refused, at the other end.
    """
    payload = beatgrid_payload(tempo_changes=0)
    payload["beats"] = [
        {"t": beat["t"] + 5.0, "n": beat["n"], "bpm": beat["bpm"]} for beat in payload["beats"]
    ]
    payload["tempo_changes"] = [
        {"at_s": 1.0, "bpm_before": 128.0, "bpm_after": 132.0, "confidence": 0.8}
    ]
    with pytest.raises(LaneContractError, match="first beat"):
        validate_lane_payload("beatgrid", payload)


def test_a_tempo_change_at_the_grids_first_beat_is_accepted() -> None:
    """Positive control: the grid's own first beat is a real beat to place it on."""
    payload = beatgrid_payload(tempo_changes=0)
    payload["beats"] = [
        {"t": beat["t"] + 5.0, "n": beat["n"], "bpm": beat["bpm"]} for beat in payload["beats"]
    ]
    first_beat_t = payload["beats"][0]["t"]
    payload["tempo_changes"] = [
        {"at_s": first_beat_t, "bpm_before": 128.0, "bpm_after": 132.0, "confidence": 0.8}
    ]
    validate_lane_payload("beatgrid", payload)


#-----------------------------------------------------------------------------
# P2 review on PR #1562: a tempo-change marker must coincide with an actual
# beat, not merely fall within the grid's [first, last] bounds
#-----------------------------------------------------------------------------

def test_a_tempo_change_between_two_beats_is_refused() -> None:
    """In-bounds and chronologically fine is not enough: the beatgrid lane's

    own producer (`apps.analysis_beatgrid.tempo_change.detect_tempo_changes`)
    only ever emits a changepoint AT `beats[split]`, so a marker sitting in
    the gap between two beats cannot have come from a real segmentation and
    identifies no beat in the supplied grid.
    """
    payload = beatgrid_payload(tempo_changes=0)
    beats = payload["beats"]
    midpoint = round((beats[10]["t"] + beats[11]["t"]) / 2, 6)
    assert beats[10]["t"] < midpoint < beats[11]["t"]
    payload["tempo_changes"] = [
        {"at_s": midpoint, "bpm_before": 128.0, "bpm_after": 132.0, "confidence": 0.8}
    ]
    with pytest.raises(LaneContractError, match="nearest beat"):
        validate_lane_payload("beatgrid", payload)


def test_a_tempo_change_on_a_mid_grid_beat_is_accepted() -> None:
    """Positive control: a marker AT an interior beat (not the first or last,

    which the bounds checks above already exercise) is still accepted.
    """
    payload = beatgrid_payload(tempo_changes=0)
    beats = payload["beats"]
    payload["tempo_changes"] = [
        {"at_s": beats[10]["t"], "bpm_before": 128.0, "bpm_after": 132.0, "confidence": 0.8}
    ]
    validate_lane_payload("beatgrid", payload)


def test_two_markers_within_tolerance_of_one_beat_are_refused() -> None:
    """Fresh Codex finding on PR #1562: the 1ms tolerance is a band around

    EACH beat, not a hard grid of allowed instants, so two markers that
    straddle one beat (`beat_t - 0.5ms`, `beat_t + 0.5ms`) each individually
    pass the nearest-beat check and are still strictly increasing -- yet
    `detect_tempo_changes` never emits two changepoints for one `beats[split]`.
    Refuse a second marker that resolves to the same (or an earlier) beat
    index as the one before it.
    """
    payload = beatgrid_payload(tempo_changes=0)
    beat_t = payload["beats"][10]["t"]
    payload["tempo_changes"] = [
        {"at_s": beat_t - 0.0005, "bpm_before": 128.0, "bpm_after": 130.0, "confidence": 0.8},
        {"at_s": beat_t + 0.0005, "bpm_before": 130.0, "bpm_after": 132.0, "confidence": 0.8},
    ]
    with pytest.raises(LaneContractError, match="same beat"):
        validate_lane_payload("beatgrid", payload)


def test_markers_on_two_different_beats_are_accepted() -> None:
    """Positive control: two markers resolving to two DIFFERENT beats, each

    within tolerance of its own beat, are still accepted.
    """
    payload = beatgrid_payload(tempo_changes=0)
    beats = payload["beats"]
    payload["tempo_changes"] = [
        {"at_s": beats[10]["t"], "bpm_before": 128.0, "bpm_after": 130.0, "confidence": 0.8},
        {"at_s": beats[20]["t"], "bpm_before": 130.0, "bpm_after": 132.0, "confidence": 0.8},
    ]
    validate_lane_payload("beatgrid", payload)


#-----------------------------------------------------------------------------
# P2 review on PR #1562: a beatgrid may not outlive its own record
#-----------------------------------------------------------------------------

def test_a_beatgrid_at_exactly_the_records_duration_is_accepted(db) -> None:
    """Positive control: a grid ending exactly on the boundary still writes."""
    payload = beatgrid_payload()  # default grid_span_s (300.0) matches own_record's duration_s
    assert payload["beats"][-1]["t"] == 300.0
    analysis_store.upsert_record(
        own_record(lane="beatgrid", result=LaneResult(status="ok", payload=payload)),
        conn=db,
    )


#-----------------------------------------------------------------------------
# P2 review on PR #1562: a key segment must be a valid range on the track
#-----------------------------------------------------------------------------

def test_a_negative_key_segment_start_s_is_refused() -> None:
    """Before the track starts is not a valid segment start, same as start_bar."""
    payload = key_payload()
    payload["segments"]["segments"][0]["start_s"] = -5.0
    with pytest.raises(LaneContractError, match="start_s"):
        validate_lane_payload("key", payload)


def test_a_key_segment_end_s_beyond_the_records_duration_is_refused(db) -> None:
    """`duration_s` is on the record, not the lane payload, so this is checked

    at the write boundary (`key_segments_within_duration`, mirroring
    `tempo_changes_within_duration`), not by `validate_lane_payload` alone --
    there is nothing to compare against without the record.
    """
    payload = key_payload()
    payload["segments"]["segments"][0]["end_s"] = 301.0  # own_record's duration_s is 300.0
    with pytest.raises(LaneContractError, match="duration_s"):
        analysis_store.upsert_record(
            own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )


def test_a_key_segment_end_s_at_the_records_duration_is_accepted(db) -> None:
    """Positive control: the boundary itself still writes."""
    payload = key_payload()
    payload["segments"]["segments"][0]["end_s"] = 300.0  # exactly own_record's duration_s
    analysis_store.upsert_record(
        own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
        conn=db,
    )


#-----------------------------------------------------------------------------
# P2 review on PR #1562: duration_s must itself be finite and nonnegative
# before the write-boundary checks compare anything against it
#-----------------------------------------------------------------------------

def test_a_nan_duration_s_is_refused() -> None:
    """NaN makes every `> duration_s` comparison evaluate False, so the

    write-boundary checks would silently pass a grid that outlives the
    record instead of bounding it.
    """
    payload = beatgrid_payload(grid_span_s=310.0)
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_lane_result(
            "beatgrid", LaneResult(status="ok", payload=payload), duration_s=float("nan")
        )


def test_an_infinite_duration_s_is_refused() -> None:
    """Same failure mode as NaN: +inf makes every bound unenforceable."""
    payload = beatgrid_payload(grid_span_s=310.0)
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_lane_result(
            "beatgrid", LaneResult(status="ok", payload=payload), duration_s=float("inf")
        )


def test_a_negative_duration_s_is_refused() -> None:
    payload = beatgrid_payload()
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_lane_result(
            "beatgrid", LaneResult(status="ok", payload=payload), duration_s=-1.0
        )


def test_a_finite_duration_s_is_still_accepted() -> None:
    """Positive control: an ordinary finite record length still validates."""
    payload = beatgrid_payload()  # default grid_span_s 300.0
    validate_lane_result(
        "beatgrid", LaneResult(status="ok", payload=payload), duration_s=300.0
    )


def test_a_nan_duration_s_is_refused_even_for_a_missing_lane() -> None:
    """Fresh Codex finding on PR #1562: the finite/nonnegative check must not

    live inside `_validate_ok_lane`, or a record whose every lane is
    `missing`/`failed` never reaches it and a broken `duration_s` (which
    `Infinity` also serializes as non-standard JSON) reaches the store
    untouched. `missing`/`failed` never touch `duration_s` themselves, so
    the SAME finite/nonnegative requirement must still fire for them.
    """
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_lane_result("key", LaneResult(status="missing"), duration_s=float("nan"))


def test_a_missing_lane_with_a_finite_duration_s_is_still_accepted() -> None:
    """Positive control: a missing lane with an ordinary duration still validates."""
    validate_lane_result("key", LaneResult(status="missing"), duration_s=300.0)
