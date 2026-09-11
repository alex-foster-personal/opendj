"""Regressions for review round eleven on PR #1562.

[if] any round-eleven review finding on PR #1562 recurs [then] fail, [else stop].

Fifth file in this series for the same reason as the fourth: keeps each
review round's regressions in their own file rather than growing one file
past the 600-line limit. Same discipline as the other four -- the claim was
RUN and the wrong behavior observed before anything changed, and each fix
goes red under a mutation back to its pre-fix source.

This round closes two classes the fan-out orchestrator asked to be audited
system-wide, not just at the two reported sites:

* Every numeric check in apps/analysis/lane_payloads.py,
  lane_payloads_shared.py and lanes.py must reject `bool` explicitly, since
  `bool` is an `int` subclass and both `math.isfinite(True)` and ordinary
  `<`/`<=`/`>`/`>=` comparisons treat it as `1` or `0`. The only site the
  audit found unguarded was `duration_s` in `validate_lane_result`
  (lanes.py); every other numeric field already routes through
  `_require_number` (which bool-guards) or has its own explicit
  `isinstance(x, bool)` check before the range comparison.
* Every ordered-sequence check must state, and enforce, whether adjacent
  elements need to MEET (share an exact boundary) or merely avoid
  overlapping. The only site the audit found stating the wrong one was key
  segments' `start_s`/`end_s` (lane_payloads.py): the bar check already
  requires an equal boundary (a MEET), but the time check only required
  non-decreasing, which left a same-bar gap between segments' timestamps
  accepted. Beat times and tempo-change markers are POINT events, not
  ranges, so "strictly increasing" is the correct (and already-enforced)
  relation for them, not a meet -- the audit confirmed those checks already
  say and enforce that, not just checked in this round.

-Claude
"""
from __future__ import annotations

import dataclasses

import pytest

from apps.analysis import store as analysis_store
from apps.analysis.lane_payloads import KEY_SEGMENT_MEET_TOLERANCE_S
from apps.analysis.lanes import (
    LaneContractError,
    LaneResult,
    validate_lane_payload,
    validate_lane_result,
)
from apps.analysis.record import RecordContractError, validate_record_contract
from tests.analysis_contract.conftest import key_payload, own_record

#-----------------------------------------------------------------------------
# P2 round 11, thread 1: adjacent key segments must MEET in time, not merely
# avoid overlapping (apps/analysis/lane_payloads.py:316)
#-----------------------------------------------------------------------------

def test_a_gap_between_key_segments_in_time_is_refused() -> None:
    """A same-bar-boundary gap in seconds belongs to no segment.

    `previous.end_s=10` and `seg.start_s=15` satisfy the old `<` check (15
    is not before 10) even though the interval [10, 15) is not covered by
    either segment; contiguous bar-synchronous ranges must share an exact
    boundary, the same requirement the bar check already enforces.
    """
    payload = key_payload(segments=2)
    segs = payload["segments"]["segments"]
    assert segs[0]["end_s"] == segs[1]["start_s"], "fixture segments meet by default"
    segs[1]["start_s"] = segs[0]["end_s"] + 5.0
    with pytest.raises(LaneContractError, match="MEET"):
        validate_lane_payload("key", payload)


def test_a_key_segment_start_s_just_outside_the_meet_tolerance_is_refused() -> None:
    """Fresh evidence beyond the coarse 5s gap above: the tolerance has an edge too."""
    payload = key_payload(segments=2)
    segs = payload["segments"]["segments"]
    segs[1]["start_s"] = segs[0]["end_s"] + 2 * KEY_SEGMENT_MEET_TOLERANCE_S
    with pytest.raises(LaneContractError, match="MEET"):
        validate_lane_payload("key", payload)


def test_a_key_segment_start_s_within_the_meet_tolerance_is_accepted() -> None:
    """Positive control: well inside the same 1ms slack tempo-change markers get.

    Not pinned to the exact tolerance boundary: `30.0 + 0.001` does not
    round-trip to a gap of exactly `0.001` in binary floating point (it
    lands fractionally past it), which would make this control flaky on the
    instrument rather than the claim -- half the tolerance has no such edge.
    """
    payload = key_payload(segments=2)
    segs = payload["segments"]["segments"]
    segs[1]["start_s"] = segs[0]["end_s"] + KEY_SEGMENT_MEET_TOLERANCE_S / 2
    validate_lane_payload("key", payload)


def test_key_segments_meeting_exactly_are_still_accepted() -> None:
    """Positive control: the untouched multi-segment fixture already meets exactly."""
    validate_lane_payload("key", key_payload(segments=4))


def test_a_gap_between_key_segments_never_reaches_the_store(db) -> None:
    """The write boundary rejects the bad block before any row is written.

    Mirrors `test_key_change_count_is_derived_only_from_a_validated_segments_block`
    in test_review_regressions_r10.py: the control that matters is that an
    invalid segments block never reaches the store at all.
    """
    payload = key_payload(segments=2)
    segs = payload["segments"]["segments"]
    segs[1]["start_s"] = segs[0]["end_s"] + 5.0
    with pytest.raises(LaneContractError, match="MEET"):
        analysis_store.upsert_record(
            own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )
    (count,) = db.execute("SELECT COUNT(*) FROM analysis").fetchone()
    assert count == 0, "the invalid segments block must never reach the store"


#-----------------------------------------------------------------------------
# P2 round 11, thread 2: a boolean duration_s must be refused explicitly
# (apps/analysis/lanes.py:156)
#-----------------------------------------------------------------------------

def test_a_true_duration_s_is_refused() -> None:
    """`bool` is an `int` subclass: `math.isfinite(True)` is `True` and

    `True < 0` is `False`, so `duration_s=True` sailed through both range
    checks before this fix and would have been stored as `1.0` in the
    scalar column while `record_json` kept the original `true`.
    """
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_lane_result("key", LaneResult(status="missing"), duration_s=True)


def test_a_false_duration_s_is_refused() -> None:
    """Same failure mode at the other boolean value: `False` reads as `0`,

    a plausible-looking (if zero-length) duration that is still not a
    number the producer ever measured.
    """
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_lane_result("key", LaneResult(status="missing"), duration_s=False)


def test_an_integer_duration_s_is_still_accepted() -> None:
    """Positive control: an ordinary (non-bool) numeric duration still validates."""
    validate_lane_result("key", LaneResult(status="missing"), duration_s=300)


def test_a_record_with_a_boolean_duration_s_is_refused_by_the_record_contract() -> None:
    """End-to-end version of the Codex scenario: a record deserialized from

    JSON with `duration_s: true` (which `AnalysisRecord.from_json` accepts
    without complaint -- dataclasses do not enforce field types at
    construction) must still be refused by `validate_record_contract`, the
    single gate `store.upsert_record` calls before every write.
    """
    record = dataclasses.replace(own_record(lane="key"), duration_s=True)
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_record_contract(record)


def test_a_record_with_a_boolean_duration_s_never_reaches_the_store(db) -> None:
    """Same scenario, through the real write path, with a store-emptiness control."""
    record = dataclasses.replace(own_record(lane="key"), duration_s=True)
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        analysis_store.upsert_record(record, conn=db)
    (count,) = db.execute("SELECT COUNT(*) FROM analysis").fetchone()
    assert count == 0, "a record with a boolean duration_s must never reach the store"


#-----------------------------------------------------------------------------
# P2 round 12, thread 1: a non-numeric duration_s must raise the contract
# error, not a raw TypeError from math.isfinite (apps/analysis/lanes.py:159)
#-----------------------------------------------------------------------------

@pytest.mark.parametrize("duration_s", ["300", b"300", [300], {"s": 300}])
def test_a_non_numeric_duration_s_raises_the_contract_error(duration_s: object) -> None:
    """`AnalysisRecord.from_json` does not enforce dataclass annotations, so a

    producer record with `duration_s: "300"` reached `math.isfinite` and
    raised a raw `TypeError` before this fix, bypassing the `ValueError`
    path callers handle for malformed records.
    """
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_lane_result("key", LaneResult(status="missing"), duration_s=duration_s)  # type: ignore[arg-type]


def test_a_record_with_a_string_duration_s_is_refused_by_the_record_contract() -> None:
    """End-to-end: the same malformed value through the single store gate."""
    record = dataclasses.replace(own_record(lane="key"), duration_s="300")  # type: ignore[arg-type]
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_record_contract(record)


#-----------------------------------------------------------------------------
# P2 round 12, thread 2: a record with `duration_s: null` must be refused by
# the record contract, since `None` is also the lane gate's "no duration
# supplied" sentinel (apps/analysis/record.py:235)
#-----------------------------------------------------------------------------

def test_a_record_with_a_null_duration_s_is_refused_by_the_record_contract() -> None:
    """`AnalysisRecord.from_json` accepts `duration_s: null`, and the lane gate

    treats `None` as "no duration to check against", so the malformed record
    passed `validate_record_contract` and `upsert_record` raised a raw
    `TypeError` at `float(record.duration_s)` instead of a contract error.
    """
    record = dataclasses.replace(own_record(lane="key"), duration_s=None)  # type: ignore[arg-type]
    with pytest.raises(RecordContractError, match="duration_s"):
        validate_record_contract(record)


def test_a_record_with_a_numeric_duration_s_still_validates() -> None:
    """Positive control: the fixture record carries a real duration and passes."""
    validate_record_contract(own_record(lane="key"))


#-----------------------------------------------------------------------------
# P2 round 12, thread 3: an integer too large for a C double raises
# OverflowError inside math.isfinite; every isfinite site shares the class
# (lanes.py:160, lane_payloads_shared.py:64, lane_payloads.py:499 and :585)
#-----------------------------------------------------------------------------

HUGE_INT = 10**400


def test_an_oversized_integer_duration_s_is_a_contract_error() -> None:
    """`math.isfinite(10**400)` raises `OverflowError`, not a contract error."""
    with pytest.raises(LaneContractError, match="finite nonnegative"):
        validate_lane_result("key", LaneResult(status="missing"), duration_s=HUGE_INT)


def test_an_oversized_integer_confidence_is_a_contract_error() -> None:
    with pytest.raises(LaneContractError, match="finite"):
        validate_lane_result(
            "key", LaneResult(status="missing", confidence=HUGE_INT)
        )


def test_an_oversized_integer_payload_number_is_a_contract_error() -> None:
    """The shared `_require_number` primitive, reached through a real lane."""
    from apps.analysis.lane_payloads_shared import _require_number

    with pytest.raises(LaneContractError, match="finite"):
        _require_number("loudness", {"integrated_lufs": HUGE_INT}, "integrated_lufs")


def test_an_oversized_integer_waveform_sample_is_a_contract_error() -> None:
    from apps.analysis.lane_payloads_shared import is_finite_number

    assert is_finite_number(HUGE_INT) is False
    assert is_finite_number(300) is True
    assert is_finite_number(300.5) is True
    assert is_finite_number(float("inf")) is False
    assert is_finite_number(float("nan")) is False
