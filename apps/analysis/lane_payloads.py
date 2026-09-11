"""Per-lane payload shapes and their range rules.

Split out of :mod:`apps.analysis.lanes`, which crossed the 600-line
per-Python-file limit as the review rounds tightened these checks. This half
is pure validation of what a producer put in a lane block; `lanes.py` keeps
the lane/producer/status enums, :class:`LaneResult`, the backend naming
scheme and semver.

Every rule here exists because something downstream would otherwise act on
the value: the deck's own `validateBeatGrid`, the projection that publishes
a scalar to the track view, `ProvenanceOut.confidence`'s `[0, 1]` semantics,
or the JSON encoder. Nothing here is defensive-by-habit.

-Claude
"""
from __future__ import annotations

import bisect
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .lane_enums import LANE_STATUSES, LANES, LaneContractError
from .lane_payloads_shared import (
    _require_bool,
    _require_confidence,
    _require_keys,
    _require_list,
    _require_number,
    _require_positive,
    _require_str,
    is_finite_number,
)

# 1ms: past the producer's round(beats[split], 4) and beats' own rounding.
TEMPO_CHANGE_BEAT_TOLERANCE_S = 0.001

# 1ms: the same serialization/rounding slack as TEMPO_CHANGE_BEAT_TOLERANCE_S,
# for the boundary two adjacent key segments are meant to share exactly.
KEY_SEGMENT_MEET_TOLERANCE_S = 0.001


def _validate_beats(beats: list[Any]) -> None:
    """The DECK's grid invariant, enforced at the write boundary.

    Transcribed from the actual consumer, `validateBeatGrid` in
    `apps/webui/frontend/src/lib/rb/beat-sync-math.ts`. Anything it throws on
    is a record that would be made canonical here and then disable or error
    the grid-dependent transport controls, so a shape the deck cannot consume
    is not `status: ok` -- it is a producer that failed, and it says so with a
    reason (Codex P2, PR #1549).

    At least two beats, `n` an INTEGER cycling 1,2,3,4, `bpm` finite and > 0,
    `t` finite, >= 0 and strictly increasing.
    """
    if len(beats) < 2:
        raise LaneContractError(
            f"beatgrid.beats has {len(beats)} beat(s); the deck requires at "
            "least 2 (beat-sync-math.ts validateBeatGrid)"
        )
    previous_t = -1.0
    previous_n = 0
    for i, beat in enumerate(beats):
        where = f"beatgrid.beats[{i}]"
        n = _validate_one_beat(where, beat)
        if i > 0:
            _validate_beat_follows(where, i, beat, n, previous_t, previous_n)
        previous_t, previous_n = float(beat["t"]), n


def _validate_one_beat(where: str, beat: Any) -> int:
    """One beat in isolation. Returns its validated bar position."""
    if not isinstance(beat, Mapping):
        raise LaneContractError(f"{where} must be a mapping, got {beat!r}")
    _require_keys(where, beat, ("t", "n", "bpm"))
    _require_number(where, beat, "t")
    _require_number(where, beat, "bpm")
    n = beat["n"]
    if isinstance(n, bool) or not isinstance(n, int):
        raise LaneContractError(f"{where}.n must be an integer 1..4, got {n!r}")
    if not 1 <= n <= 4:
        raise LaneContractError(f"{where}.n must be within 1..4, got {n!r}")
    if beat["bpm"] <= 0:
        raise LaneContractError(f"{where}.bpm must be > 0, got {beat['bpm']!r}")
    if beat["t"] < 0:
        raise LaneContractError(f"{where}.t must be >= 0, got {beat['t']!r}")
    return n


def _validate_beat_follows(
    where: str, i: int, beat: Mapping[str, Any], n: int,
    previous_t: float, previous_n: int,
) -> None:
    """One beat relative to the one before it: time order and bar cadence."""
    if beat["t"] <= previous_t:
        raise LaneContractError(
            f"beatgrid.beats times must strictly increase: [{i - 1}].t="
            f"{previous_t}, {where}.t={beat['t']}"
        )
    expected = 1 if previous_n == 4 else previous_n + 1
    if n != expected:
        raise LaneContractError(
            f"beatgrid.beats n must cycle 1,2,3,4: {where}.n={n}, expected {expected}"
        )


def _nearest_beat(beat_times: Sequence[float], at_s: float) -> tuple[int, float]:
    """The index of the closest beat to ``at_s``, and ``|at_s - t|`` to it.

    ``beat_times`` is sorted (non-empty); returns the SAME index for a tie,
    so two markers within tolerance of one beat resolve to one index and
    the caller can refuse them as duplicates of that beat.
    """
    idx = bisect.bisect_left(beat_times, at_s)
    candidates = []
    if idx < len(beat_times):
        candidates.append((idx, abs(beat_times[idx] - at_s)))
    if idx > 0:
        candidates.append((idx - 1, abs(beat_times[idx - 1] - at_s)))
    return min(candidates, key=lambda pair: pair[1])


def _validate_tempo_change_marker(
    where: str,
    change: Mapping[str, Any],
    previous_at_s: float | None,
    previous_beat_index: int | None,
    first_beat_t: float,
    last_beat_t: float,
    beat_times: Sequence[float],
) -> tuple[float, int]:
    """One tempo-change marker: keys, magnitudes, and its place on the timeline.

    Returns its own ``at_s`` and matched beat index so the caller can thread
    both in as the next marker's ``previous_at_s``/``previous_beat_index``.
    """
    _require_keys(where, change, ("at_s", "bpm_before", "bpm_after", "confidence"))
    for key in ("bpm_before", "bpm_after"):
        _require_positive(where, change, key)
    _require_number(where, change, "at_s")
    at_s = float(change["at_s"])
    if at_s < 0:
        raise LaneContractError(f"{where}.at_s must be >= 0, got {at_s!r}")
    if at_s < first_beat_t:
        # Leading silence: the grid's first beat can start after t=0, and a
        # marker before it has no beat under it either -- the same reason a
        # marker past the last beat is refused, at the other end.
        raise LaneContractError(
            f"{where}.at_s is {at_s!r}, before beatgrid.beats' first beat at "
            f"{first_beat_t!r}; a tempo change needs a beat to place it on"
        )
    if at_s > last_beat_t:
        # A marker past the grid's own last beat has no beat left to place
        # it on -- the deck locates a tempo change AT a beat, not in a gap
        # past where the grid stops.
        raise LaneContractError(
            f"{where}.at_s is {at_s!r}, beyond beatgrid.beats' last beat at "
            f"{last_beat_t!r}; a tempo change needs a beat to place it on"
        )
    if previous_at_s is not None and at_s <= previous_at_s:
        # One timeline, one direction: a marker at or before its predecessor
        # cannot describe a tempo change the deck can place.
        raise LaneContractError(
            f"{where}.at_s must strictly increase from the previous marker: "
            f"got {at_s!r} after {previous_at_s!r}"
        )
    beat_index, gap = _nearest_beat(beat_times, at_s)
    if gap > TEMPO_CHANGE_BEAT_TOLERANCE_S:
        # In-bounds isn't enough: tempo_change.py's detect_tempo_changes only
        # ever emits a marker AT beats[split] (Codex P2, PR #1562).
        raise LaneContractError(
            f"{where}.at_s is {at_s!r}, {gap:.4f}s from the nearest beat in "
            "beatgrid.beats; a tempo change must coincide with an actual beat"
        )
    if previous_beat_index is not None and beat_index <= previous_beat_index:
        # Two markers within tolerance of the SAME beat both pass the checks
        # above (each is close to some beat, and their raw at_s can still
        # strictly increase) but detect_tempo_changes emits one changepoint
        # per split, never two for the same beat (Codex P2, PR #1562).
        raise LaneContractError(
            f"{where} resolves to beat index {beat_index}, the same beat (or "
            f"an earlier one) as the previous marker's index {previous_beat_index}; "
            "each tempo change must land on a different beat"
        )
    _require_confidence(where, change, "confidence")
    return at_s, beat_index


def _validate_beatgrid(payload: Mapping[str, Any]) -> None:
    _require_keys("beatgrid", payload, (
        "beats", "bpm", "bpm_confidence", "octave_reason", "first_downbeat_s",
        "tempo_changes", "static_grid_untrusted",
    ))
    beats = _require_list("beatgrid", payload, "beats")
    if not beats:
        # Spec section 3: "An empty beatgrid.beats with status: ok is a
        # contract violation, not a state." A lane that found no pulse says
        # so with status failed and no_trackable_pulse.
        raise LaneContractError(
            "beatgrid.beats is empty with status ok; a lane that found no pulse "
            "records status failed with reason no_trackable_pulse instead"
        )
    _validate_beats(beats)
    # The PROJECTED bpm: `_project_lane` publishes this exact value to the
    # track view and to smartlists, so a zero or negative here would make the
    # public BPM disagree with the beat intervals from its own lane.
    _require_positive("beatgrid", payload, "bpm")
    _require_confidence("beatgrid", payload, "bpm_confidence")
    _require_str("beatgrid", payload, "octave_reason")
    _require_number("beatgrid", payload, "first_downbeat_s")
    _require_bool("beatgrid", payload, "static_grid_untrusted")
    first_beat_t = float(beats[0]["t"])
    last_beat_t = float(beats[-1]["t"])
    beat_times = [float(beat["t"]) for beat in beats]  # sorted; _validate_beats enforces it
    previous_at_s: float | None = None
    previous_beat_index: int | None = None
    for i, change in enumerate(_require_list("beatgrid", payload, "tempo_changes")):
        if not isinstance(change, Mapping):
            raise LaneContractError(
                f"beatgrid.tempo_changes[{i}] must be a mapping, got {change!r}"
            )
        where = f"beatgrid.tempo_changes[{i}]"
        previous_at_s, previous_beat_index = _validate_tempo_change_marker(
            where, change, previous_at_s, previous_beat_index,
            first_beat_t, last_beat_t, beat_times,
        )


def beats_within_duration(payload: Mapping[str, Any], duration_s: float) -> None:
    """Write-boundary check: the grid itself may not outlive the record.

    Checking only the LAST beat is sufficient, not a shortcut: `_validate_beats`
    already established the timestamps are strictly increasing, so if the last
    one is within `duration_s` every earlier one is too.
    """
    beats = payload.get("beats", ())
    if not beats:
        return
    last_t = float(beats[-1]["t"])
    if last_t > duration_s:
        raise LaneContractError(
            f"beatgrid.beats[{len(beats) - 1}].t is {last_t!r}, beyond the "
            f"record's duration_s {duration_s!r}"
        )


def tempo_changes_within_duration(payload: Mapping[str, Any], duration_s: float) -> None:
    """Write-boundary check: no marker may lie past the record's own duration.

    In a grid `beats_within_duration` has already accepted, this branch is
    unreachable -- a marker beyond `duration_s` is then also beyond the
    grid's own last beat, which `_validate_beatgrid` refuses on its own. It
    stays as the write-boundary counterpart to that payload-level check
    (`tests/analysis_contract/test_review_regressions_r10.py` exercises it
    directly for that reason) rather than something a valid payload can
    still trigger.
    """
    for i, change in enumerate(payload.get("tempo_changes", ())):
        at_s = float(change["at_s"])
        if at_s > duration_s:
            raise LaneContractError(
                f"beatgrid.tempo_changes[{i}].at_s is {at_s!r}, beyond the "
                f"record's duration_s {duration_s!r}"
            )


# 1..12 followed by A (minor) or B (major). The deck's own parser is
# `parseCamelotKey` in apps/webui/frontend/src/lib/player/key/camelot.ts; it
# returns null for anything else, which silently disables Key Sync and
# harmonic compatibility while provenance still claims `status: ok` (Codex
# P2, PR #1549). A key the deck cannot parse is a failed measurement.
_CAMELOT_RE = re.compile(r"^(?:[1-9]|1[0-2])[AB]$")

# Camelot wheel -> (pitch class, is_minor). 8A is A minor, 8B is C major.
_CAMELOT_PITCH_CLASS: dict[str, tuple[int, bool]] = {
    f"{n}{mode}": (pc, mode == "A")
    for mode, roots in (
        # Minor keys around the wheel from 1A = A-flat minor (pc 8).
        ("A", [8, 3, 10, 5, 0, 7, 2, 9, 4, 11, 6, 1]),
        # Major keys from 1B = B major (pc 11).
        ("B", [11, 6, 1, 8, 3, 10, 5, 0, 7, 2, 9, 4]),
    )
    for n, pc in enumerate(roots, start=1)
}


def _require_camelot(lane: str, payload: Mapping[str, Any], key: str) -> None:
    _require_str(lane, payload, key)
    value = payload[key]
    if not _CAMELOT_RE.match(value):
        raise LaneContractError(
            f"{lane}.{key} is {value!r}, which is not a Camelot key (1..12 "
            "followed by A or B); the deck's parseCamelotKey returns null for "
            "it and silently disables Key Sync"
        )
    expected_pc, expected_minor = _CAMELOT_PITCH_CLASS[value]
    if payload.get("pitch_class") != expected_pc:
        raise LaneContractError(
            f"{lane}.camelot {value!r} is pitch class {expected_pc}, but the "
            f"record says {payload.get('pitch_class')!r}; the two spellings of "
            "one measurement must agree"
        )
    if payload.get("is_minor") != expected_minor:
        raise LaneContractError(
            f"{lane}.camelot {value!r} is {'minor' if expected_minor else 'major'}, "
            f"but is_minor says {payload.get('is_minor')!r}"
        )


def _validate_key_segment_order(
    where: str, seg: Mapping[str, Any], previous: Mapping[str, Any]
) -> None:
    """One key segment relative to the one before it: bar and time order.

    Segments are bar-synchronous timeline RANGES, not point events, so
    adjacent segments must MEET: the next `start_s` equal to the previous
    `end_s`, within `KEY_SEGMENT_MEET_TOLERANCE_S`, not merely avoid
    overlapping. `start_s=15` after `previous.end_s=10` passes a bare `<`
    check but leaves the 10-15s interval belonging to no segment, exactly
    like the bar check already requires an equal boundary rather than a
    non-decreasing one (Codex P2, PR #1562, round three).
    """
    if seg["start_bar"] != previous["end_bar"]:
        raise LaneContractError(
            f"{where}.start_bar is {seg['start_bar']!r} but the previous "
            f"segment ends at bar {previous['end_bar']!r}; segments must be "
            "contiguous in bar order, with no overlap and no gap"
        )
    gap = float(seg["start_s"]) - float(previous["end_s"])
    if abs(gap) > KEY_SEGMENT_MEET_TOLERANCE_S:
        raise LaneContractError(
            f"{where}.start_s is {seg['start_s']!r} but the previous segment's "
            f"end_s is {previous['end_s']!r}; adjacent segments must MEET in "
            "time (equal start_s/end_s within 1ms), not merely avoid overlapping"
        )


def _validate_key_segments(segments_block: Any) -> None:
    if not isinstance(segments_block, Mapping):
        raise LaneContractError(f"key.segments must be a mapping, got {segments_block!r}")
    _require_keys("key.segments", segments_block, ("status", "reason", "segments"))
    status = segments_block["status"]
    if status not in LANE_STATUSES:
        raise LaneContractError(
            f"key.segments.status must be one of {LANE_STATUSES}, got {status!r}"
        )
    if status == "failed" and not segments_block.get("reason"):
        # Same rule as a failed LANE, for the same reason: the projection
        # copies this reason into `key_change_count`, so a failed block with
        # no reason loses the analyzer's actual failure permanently and the
        # read model can only show a generic placeholder.
        raise LaneContractError(
            "key.segments.status is failed without a reason; the read model "
            "surfaces that reason and cannot invent one"
        )
    segments = _require_list("key.segments", segments_block, "segments")
    if status == "ok" and not segments:
        raise LaneContractError(
            "key.segments.status is ok with no segments; a stable key is one "
            "segment, and an unavailable analysis is status missing"
        )
    if status != "ok" and segments:
        raise LaneContractError(
            f"key.segments.status is {status!r} but carries {len(segments)} segments"
        )
    previous: Mapping[str, Any] | None = None
    for i, seg in enumerate(segments):
        where = f"key.segments.segments[{i}]"
        _validate_one_key_segment(where, seg)
        if previous is not None:
            _validate_key_segment_order(where, seg, previous)
        previous = seg


def _validate_key_segment_bounds(where: str, seg: Mapping[str, Any]) -> None:
    """Bar and time bounds for one key segment: sane types, sane ranges.

    ``start_s`` >= 0 for the same reason a bar index >= 0: a segment cannot
    start before the track does. ``end_s`` against the record's own
    ``duration_s`` is a separate, write-boundary check in
    :func:`key_segments_within_duration` -- there is nothing to compare
    against without the record.
    """
    for key in ("start_bar", "end_bar"):
        value = seg[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise LaneContractError(f"{where}.{key} must be an integer bar index, got {value!r}")
    if seg["start_bar"] < 0:
        raise LaneContractError(f"{where}.start_bar must be >= 0, got {seg['start_bar']!r}")
    if not seg["start_bar"] < seg["end_bar"]:
        raise LaneContractError(
            f"{where}.start_bar must be < end_bar, got {seg['start_bar']!r} and {seg['end_bar']!r}"
        )
    for key in ("start_s", "end_s"):
        _require_number(where, seg, key)
    if float(seg["start_s"]) < 0:
        raise LaneContractError(f"{where}.start_s must be >= 0, got {seg['start_s']!r}")
    if not float(seg["start_s"]) < float(seg["end_s"]):
        raise LaneContractError(
            f"{where}.start_s must be < end_s, got {seg['start_s']!r} and {seg['end_s']!r}"
        )


def _validate_one_key_segment(where: str, seg: Any) -> None:
    """One bar-indexed key segment: bounds, its own Camelot key, confidence."""
    if not isinstance(seg, Mapping):
        raise LaneContractError(f"{where} must be a mapping")
    _require_keys(where, seg, (
        "start_bar", "end_bar", "start_s", "end_s",
        "key_camelot", "key_openkey", "confidence",
    ))
    _validate_key_segment_bounds(where, seg)
    _require_confidence(where, seg, "confidence")
    _require_str(where, seg, "key_camelot")
    if not _CAMELOT_RE.match(seg["key_camelot"]):
        raise LaneContractError(
            f"{where}.key_camelot is {seg['key_camelot']!r}, which is not a "
            "Camelot key"
        )
    _require_str(where, seg, "key_openkey")


def key_segments_within_duration(payload: Mapping[str, Any], duration_s: float) -> None:
    """Write-boundary check: no segment's end may lie past the record's own duration.

    Threaded in the same way :func:`tempo_changes_within_duration` is: bar
    and start_s bounds are checked at the shape level in
    :func:`_validate_key_segment_bounds`, but `duration_s` lives on the
    record, not the lane payload, so there is nothing to compare `end_s`
    against without it.
    """
    segments_block = payload.get("segments")
    if not isinstance(segments_block, Mapping):
        return
    for i, seg in enumerate(segments_block.get("segments", ())):
        end_s = float(seg["end_s"])
        if end_s > duration_s:
            raise LaneContractError(
                f"key.segments.segments[{i}].end_s is {end_s!r}, beyond the "
                f"record's duration_s {duration_s!r}"
            )


def _validate_key(payload: Mapping[str, Any]) -> None:
    _require_keys("key", payload, (
        "camelot", "openkey", "pitch_class", "is_minor", "confidence", "segments",
    ))
    _require_str("key", payload, "openkey")
    # ORDER MATTERS: each field is checked on its own terms BEFORE the
    # cross-check that they agree, so a bad pitch class reports as a bad
    # pitch class rather than as a disagreement with a perfectly good key.
    # An INTEGER, not a number that rounds into range. Pitch class is the
    # discrete identity the key and mode bit are read against, so `-0.5` and
    # `11.9` are malformed input, not edge values: int() would have turned
    # them into 0 and 11 and stored a valid-looking record.
    pitch_class = payload["pitch_class"]
    if isinstance(pitch_class, bool) or not isinstance(pitch_class, int):
        raise LaneContractError(
            f"key.pitch_class must be an integer 0..11, got {pitch_class!r}"
        )
    if not 0 <= pitch_class <= 11:
        raise LaneContractError(f"key.pitch_class must be 0..11, got {pitch_class!r}")
    _require_bool("key", payload, "is_minor")
    _require_camelot("key", payload, "camelot")
    _require_confidence("key", payload, "confidence")
    _validate_key_segments(payload["segments"])


def _validate_band_block(where: str, block: Any) -> None:
    if not isinstance(block, Mapping):
        raise LaneContractError(f"{where} must be a mapping, got {block!r}")
    _require_keys(where, block, ("length", "low", "mid", "high"))
    length = block["length"]
    # An INTEGER, compared directly. `int(3.9)` is 3, so a block declaring
    # 3.9 with three samples used to pass and store a length contradicting
    # its own arrays, which a consumer scaling by that length would then act
    # on (Codex P2, PR #1549).
    if isinstance(length, bool) or not isinstance(length, int):
        raise LaneContractError(
            f"{where}.length must be a non-negative integer, got {length!r}"
        )
    if length < 0:
        raise LaneContractError(f"{where}.length must be >= 0, got {length!r}")
    for band in ("low", "mid", "high"):
        values = _require_list(where, block, band)
        if len(values) != length:
            raise LaneContractError(
                f"{where}.{band} has {len(values)} samples but length says {length}"
            )
        # Every SAMPLE, not just the count. A band is a measurement like any
        # other number on any other lane: a string, a NaN or an infinity in
        # here reaches the renderer and the API's JSON encoder, and the count
        # check alone would have waved all three through.
        for i, sample in enumerate(values):
            if isinstance(sample, bool) or not isinstance(sample, (int, float)):
                raise LaneContractError(
                    f"{where}.{band}[{i}] must be a number, got {sample!r}"
                )
            if not is_finite_number(sample):
                raise LaneContractError(
                    f"{where}.{band}[{i}] is {sample!r}, which is not a finite "
                    "measurement"
                )


def _validate_waveform(payload: Mapping[str, Any]) -> None:
    _require_keys("waveform", payload, ("kind", "preview", "detail"))
    if payload["kind"] != "tri":
        raise LaneContractError(
            f"waveform.kind must be 'tri' for an own record, got {payload['kind']!r}"
        )
    _validate_band_block("waveform.preview", payload["preview"])
    _validate_band_block("waveform.detail", payload["detail"])


def _validate_loudness(payload: Mapping[str, Any]) -> None:
    keys = ("integrated_lufs", "true_peak_dbtp", "loudness_range_lu", "rms_db")
    _require_keys("loudness", payload, keys)
    for key in keys:
        _require_number("loudness", payload, key)


def _validate_vocal(payload: Mapping[str, Any]) -> None:
    """The vocal lane is the shipped stems lane, unchanged (spec section 2).

    v1 does not redefine its payload, so this accepts any mapping rather
    than inventing a shape for a lane this milestone does not own. The
    status/reason discipline in :func:`validate_lane_result` still applies.
    """
    if not isinstance(payload, Mapping):
        raise LaneContractError(f"vocal payload must be a mapping, got {payload!r}")


_LANE_VALIDATORS = {
    "beatgrid": _validate_beatgrid,
    "key": _validate_key,
    "waveform": _validate_waveform,
    "loudness": _validate_loudness,
    "vocal": _validate_vocal,
}


def validate_lane_payload(lane: str, payload: Mapping[str, Any]) -> None:
    """Pure shape check for one lane's ``ok`` payload. Raises, returns None.

    Shapes (spec section 3 and the lane briefs):

    * ``beatgrid``: ``beats[{t, n, bpm}]`` (never empty), ``bpm``,
      ``bpm_confidence``, ``octave_reason``, ``first_downbeat_s``,
      ``tempo_changes[{at_s, bpm_before, bpm_after, confidence}]``,
      ``static_grid_untrusted``.
    * ``key``: ``camelot``, ``openkey``, ``pitch_class`` (0..11),
      ``is_minor``, ``confidence``, ``segments{status, reason, segments[
      {start_bar, end_bar, start_s, end_s, key_camelot, key_openkey,
      confidence}]}``.
    * ``waveform``: ``kind`` (always ``tri``), ``preview{length, low, mid,
      high}``, ``detail{...}``.
    * ``loudness``: ``integrated_lufs``, ``true_peak_dbtp``,
      ``loudness_range_lu``, ``rms_db``.
    * ``vocal``: unchanged from the shipped stems lane, so unconstrained.
    """
    validator = _LANE_VALIDATORS.get(lane)
    if validator is None:
        raise LaneContractError(f"unknown lane {lane!r}; lanes are {LANES}")
    if not isinstance(payload, Mapping):
        raise LaneContractError(f"{lane} payload must be a mapping, got {payload!r}")
    validator(payload)


def check_lane_confidence(lane: str, confidence: float | None) -> None:
    """`None` or a finite number.

    It is copied verbatim into ``analysis_projection.confidence`` and from
    there into ``ProvenanceOut.confidence`` (``float | None``), so a string
    reaches pydantic and a non-finite float reaches the JSON encoder, turning
    a valid track response into an error. It is a measurement like every
    other number on every other lane and is held to the same rule.
    """
    if confidence is None:
        return
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise LaneContractError(
            f"{lane}.confidence must be a number or None, got {confidence!r}"
        )
    if not is_finite_number(confidence):
        raise LaneContractError(
            f"{lane}.confidence is {confidence!r}, which is not a finite measurement"
        )
    if not 0.0 <= confidence <= 1.0:
        raise LaneContractError(
            f"{lane}.confidence is {confidence!r}; a confidence is a probability "
            "in [0, 1], the same interval track_fields provenance uses"
        )


__all__ = ["check_lane_confidence", "validate_lane_payload"]
