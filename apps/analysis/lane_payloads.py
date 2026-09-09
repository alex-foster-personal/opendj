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

import math
from collections.abc import Mapping
from typing import Any

from .lane_enums import LANE_STATUSES, LANES, LaneContractError


def _require_keys(lane: str, payload: Mapping[str, Any], keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if k not in payload]
    if missing:
        raise LaneContractError(
            f"{lane} payload is missing required keys {missing}; got {sorted(payload)}"
        )


def _require_confidence(where: str, payload: Mapping[str, Any], key: str) -> None:
    """A finite number in [0, 1].

    `track_fields` provenance already constrains confidence to that interval
    and own values reach the SAME `ProvenanceOut.confidence`, so a producer
    emitting -0.2 or 1.2 would give own analysis different public semantics
    from every other source (Codex P2, PR #1549).
    """
    _require_number(where, payload, key)
    if not 0.0 <= payload[key] <= 1.0:
        raise LaneContractError(
            f"{where}.{key} is {payload[key]!r}; a confidence is a probability "
            "in [0, 1]"
        )


def _require_positive(where: str, payload: Mapping[str, Any], key: str) -> None:
    _require_number(where, payload, key)
    if payload[key] <= 0:
        raise LaneContractError(f"{where}.{key} must be > 0, got {payload[key]!r}")


def _require_number(lane: str, payload: Mapping[str, Any], key: str) -> None:
    """A number, and a FINITE one.

    NaN and the infinities are `float` instances, so an isinstance check
    alone accepts them and the lane stores `status: ok`. SQLite then binds
    NaN as NULL, which produces an `ok` projection row with no value -- the
    exact shape this contract exists to make impossible -- and an infinity
    breaks JSON serialization on the way out of the API instead. A DSP
    producer that divided by zero has FAILED; it says so with
    `status: failed` and a reason, not with a number that is not one.
    """
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LaneContractError(f"{lane}.{key} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise LaneContractError(
            f"{lane}.{key} is {value!r}, which is not a finite measurement; a "
            "producer that could not measure records status failed with a reason"
        )


def _require_bool(lane: str, payload: Mapping[str, Any], key: str) -> None:
    if not isinstance(payload[key], bool):
        raise LaneContractError(f"{lane}.{key} must be a bool, got {payload[key]!r}")


def _require_str(lane: str, payload: Mapping[str, Any], key: str) -> None:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise LaneContractError(f"{lane}.{key} must be a non-empty string, got {value!r}")


def _require_list(lane: str, payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload[key]
    if not isinstance(value, list):
        raise LaneContractError(f"{lane}.{key} must be a list, got {value!r}")
    return value


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
    for i, change in enumerate(_require_list("beatgrid", payload, "tempo_changes")):
        if not isinstance(change, Mapping):
            raise LaneContractError(
                f"beatgrid.tempo_changes[{i}] must be a mapping, got {change!r}"
            )
        where = f"beatgrid.tempo_changes[{i}]"
        _require_keys(where, change, ("at_s", "bpm_before", "bpm_after", "confidence"))
        for key in ("bpm_before", "bpm_after"):
            _require_positive(where, change, key)
        _require_number(where, change, "at_s")
        _require_confidence(where, change, "confidence")


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
    for i, seg in enumerate(segments):
        if not isinstance(seg, Mapping):
            raise LaneContractError(f"key.segments.segments[{i}] must be a mapping")
        where = f"key.segments.segments[{i}]"
        _require_keys(where, seg, (
            "start_bar", "end_bar", "start_s", "end_s",
            "key_camelot", "key_openkey", "confidence",
        ))
        for key in ("start_bar", "end_bar", "start_s", "end_s"):
            _require_number(where, seg, key)
        _require_confidence(where, seg, "confidence")
        _require_str(where, seg, "key_camelot")
        _require_str(where, seg, "key_openkey")


def _validate_key(payload: Mapping[str, Any]) -> None:
    _require_keys("key", payload, (
        "camelot", "openkey", "pitch_class", "is_minor", "confidence", "segments",
    ))
    _require_str("key", payload, "camelot")
    _require_str("key", payload, "openkey")
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
            if not math.isfinite(sample):
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
    if not math.isfinite(confidence):
        raise LaneContractError(
            f"{lane}.confidence is {confidence!r}, which is not a finite measurement"
        )
    if not 0.0 <= confidence <= 1.0:
        raise LaneContractError(
            f"{lane}.confidence is {confidence!r}; a confidence is a probability "
            "in [0, 1], the same interval track_fields provenance uses"
        )


__all__ = ["check_lane_confidence", "validate_lane_payload"]
