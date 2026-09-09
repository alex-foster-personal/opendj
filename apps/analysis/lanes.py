"""Selection lanes, lane results, and the own-producer backend naming scheme.

`specs/native-analysis-v1.md` section 3 splits native analysis into five
SELECTION LANES (the packages of D5, not the scoring rows of section 2):
``beatgrid`` (grid, downbeat, BPM and tempo-change together), ``key`` (key
and key-change), ``waveform``, ``loudness`` and ``vocal``. Grid and BPM are
one lane on purpose: a beat entry carries both its time and its local BPM,
so a separate BPM toggle would let a composed read mix rekordbox BPM onto
own beat times.

Two things live here because both the record and the store need them and
neither owns the other:

* :class:`LaneResult` plus :func:`validate_lane_payload`, the pure
  shape check for what a producer put in a lane block.
* The own-producer backend naming scheme, ``own_<lane>.<producer>`` and
  ``own_<lane>.cand.<name>``, with :func:`parse_own_backend` as its only
  reader. Producer identity is part of the ``analysis`` primary key, so
  rows never overwrite across producers and run order cannot change what
  exists (spec section 3, "Record").

Semver lives here too: the canonical pointer ranks own rows by
``backend_version``, so that version has to be an ordered value rather
than the free-form build string the pre-v1 backends wrote
(``librosa==0.10.2.post1+madmom==0.17.dev0``). :func:`semver_key` refuses
anything it cannot order instead of sorting it lexically.

-Claude
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Lane = Literal["beatgrid", "key", "waveform", "loudness", "vocal"]
LANES: tuple[Lane, ...] = ("beatgrid", "key", "waveform", "loudness", "vocal")

LaneStatus = Literal["ok", "failed", "missing"]
LANE_STATUSES: tuple[LaneStatus, ...] = ("ok", "failed", "missing")

Producer = Literal["inapp", "backfill", "cand"]
PRODUCERS: tuple[Producer, ...] = ("inapp", "backfill", "cand")

OWN_BACKEND_PREFIX = "own_"


class LaneContractError(ValueError):
    """A lane block violates the v1 record contract."""


#-----------------------------------------------------------------------------
# lane result
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class LaneResult:
    """One lane's outcome inside an :class:`~apps.analysis.record.AnalysisRecord`.

    ``status`` is the whole point: a lane that could not measure records
    ``failed`` plus the lane's named reason (``no_trackable_pulse``,
    ``not_decoded``, ``no_tonal_center``, ...), and a lane that has not run
    records ``missing``. Neither is ever rendered as a value, so a consumer
    can tell a failed analysis from an unanalyzed track without guessing.
    """

    status: LaneStatus
    reason: str | None = None
    confidence: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "confidence": self.confidence,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LaneResult:
        return cls(
            status=data["status"],
            reason=data.get("reason"),
            confidence=data.get("confidence"),
            payload=dict(data.get("payload") or {}),
        )


#-----------------------------------------------------------------------------
# lane payload shapes
#-----------------------------------------------------------------------------

def _require_keys(lane: str, payload: Mapping[str, Any], keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if k not in payload]
    if missing:
        raise LaneContractError(
            f"{lane} payload is missing required keys {missing}; got {sorted(payload)}"
        )


def _require_number(lane: str, payload: Mapping[str, Any], key: str) -> None:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LaneContractError(f"{lane}.{key} must be a number, got {value!r}")


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
    for i, beat in enumerate(beats):
        if not isinstance(beat, Mapping):
            raise LaneContractError(f"beatgrid.beats[{i}] must be a mapping, got {beat!r}")
        _require_keys(f"beatgrid.beats[{i}]", beat, ("t", "n", "bpm"))
        _require_number(f"beatgrid.beats[{i}]", beat, "t")
        _require_number(f"beatgrid.beats[{i}]", beat, "n")
        _require_number(f"beatgrid.beats[{i}]", beat, "bpm")
    _require_number("beatgrid", payload, "bpm")
    _require_number("beatgrid", payload, "bpm_confidence")
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
        for key in ("at_s", "bpm_before", "bpm_after", "confidence"):
            _require_number(where, change, key)


def _validate_key_segments(segments_block: Any) -> None:
    if not isinstance(segments_block, Mapping):
        raise LaneContractError(f"key.segments must be a mapping, got {segments_block!r}")
    _require_keys("key.segments", segments_block, ("status", "reason", "segments"))
    status = segments_block["status"]
    if status not in LANE_STATUSES:
        raise LaneContractError(
            f"key.segments.status must be one of {LANE_STATUSES}, got {status!r}"
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
        for key in ("start_bar", "end_bar", "start_s", "end_s", "confidence"):
            _require_number(where, seg, key)
        _require_str(where, seg, "key_camelot")
        _require_str(where, seg, "key_openkey")


def _validate_key(payload: Mapping[str, Any]) -> None:
    _require_keys("key", payload, (
        "camelot", "openkey", "pitch_class", "is_minor", "confidence", "segments",
    ))
    _require_str("key", payload, "camelot")
    _require_str("key", payload, "openkey")
    _require_number("key", payload, "pitch_class")
    if not 0 <= int(payload["pitch_class"]) <= 11:
        raise LaneContractError(
            f"key.pitch_class must be 0..11, got {payload['pitch_class']!r}"
        )
    _require_bool("key", payload, "is_minor")
    _require_number("key", payload, "confidence")
    _validate_key_segments(payload["segments"])


def _validate_band_block(where: str, block: Any) -> None:
    if not isinstance(block, Mapping):
        raise LaneContractError(f"{where} must be a mapping, got {block!r}")
    _require_keys(where, block, ("length", "low", "mid", "high"))
    _require_number(where, block, "length")
    for band in ("low", "mid", "high"):
        values = _require_list(where, block, band)
        if len(values) != int(block["length"]):
            raise LaneContractError(
                f"{where}.{band} has {len(values)} samples but length says "
                f"{block['length']}"
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


def validate_lane_result(lane: str, result: LaneResult) -> None:
    """Check one lane block: status, reason discipline, and payload shape."""
    if lane not in LANES:
        raise LaneContractError(f"unknown lane {lane!r}; lanes are {LANES}")
    if result.status not in LANE_STATUSES:
        raise LaneContractError(
            f"{lane}.status must be one of {LANE_STATUSES}, got {result.status!r}"
        )
    if result.status == "failed":
        if not result.reason:
            raise LaneContractError(
                f"{lane}.status is failed without a reason; a failed lane names "
                "why (no_trackable_pulse, not_decoded, no_tonal_center, ...)"
            )
        if result.payload:
            raise LaneContractError(
                f"{lane}.status is failed but carries a payload; a failed lane "
                "has no measurement to serve"
            )
    elif result.status == "missing":
        if result.payload:
            raise LaneContractError(
                f"{lane}.status is missing but carries a payload"
            )
    elif result.status == "ok":
        validate_lane_payload(lane, result.payload)


#-----------------------------------------------------------------------------
# backend naming
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class OwnBackend:
    """The parsed halves of an ``own_<lane>.<producer>`` backend name."""

    lane: str
    producer: Producer
    candidate: str | None = None


_CAND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def own_backend(lane: str, producer: Producer, candidate: str | None = None) -> str:
    """Build the backend name for an own producer's rows.

    ``own_<lane>.<producer>`` for the two shipping producers, and
    ``own_<lane>.cand.<name>`` for a bench candidate, which the canonical
    pointer never selects.
    """
    if lane not in LANES:
        raise LaneContractError(f"unknown lane {lane!r}; lanes are {LANES}")
    if producer not in PRODUCERS:
        raise LaneContractError(f"unknown producer {producer!r}; producers are {PRODUCERS}")
    if producer == "cand":
        if not candidate or not _CAND_NAME_RE.match(candidate):
            raise LaneContractError(
                f"a cand backend needs a [a-z0-9_] candidate name, got {candidate!r}"
            )
        return f"{OWN_BACKEND_PREFIX}{lane}.cand.{candidate}"
    if candidate is not None:
        raise LaneContractError(
            f"producer {producer!r} takes no candidate name, got {candidate!r}"
        )
    return f"{OWN_BACKEND_PREFIX}{lane}.{producer}"


def parse_own_backend(backend: str) -> OwnBackend | None:
    """Parse an own backend name, or return ``None`` for a pre-v1 backend.

    ``None`` is the answer for ``librosa-only`` and ``librosa+madmom``,
    the two backends the 347 stored rows on this machine were written by:
    they predate the v1 contract and are not own records, so they are not
    subject to it and are never eligible for the canonical pointer.
    """
    if not backend.startswith(OWN_BACKEND_PREFIX):
        return None
    body = backend[len(OWN_BACKEND_PREFIX):]
    lane, _, tail = body.partition(".")
    if lane not in LANES or not tail:
        raise LaneContractError(
            f"backend {backend!r} starts with {OWN_BACKEND_PREFIX!r} but is not a "
            f"valid own backend name (own_<lane>.<producer> with lane in {LANES})"
        )
    if tail in ("inapp", "backfill"):
        return OwnBackend(lane=lane, producer=tail, candidate=None)  # type: ignore[arg-type]
    prefix, _, candidate = tail.partition(".")
    if prefix != "cand" or not candidate or not _CAND_NAME_RE.match(candidate):
        raise LaneContractError(
            f"backend {backend!r} is neither own_<lane>.inapp/.backfill nor "
            "own_<lane>.cand.<name>"
        )
    return OwnBackend(lane=lane, producer="cand", candidate=candidate)


#-----------------------------------------------------------------------------
# semver
#-----------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9a-zA-Z.-]+))?$"
)


class SemverError(ValueError):
    """A version string the canonical pointer cannot order."""


def semver_key(version: str) -> tuple[int, int, int, int, tuple[Any, ...]]:
    """Ordering key for an own producer's ``backend_version``.

    Refuses anything that is not ``MAJOR.MINOR.PATCH`` with an optional
    prerelease, because the alternative is a lexical sort that would rank
    ``0.10.0`` below ``0.9.0`` and pick the wrong canonical row in silence.
    A release outranks its own prereleases (semver 2.0.0 rule 11).
    """
    match = _SEMVER_RE.match(version)
    if match is None:
        raise SemverError(
            f"own backend_version {version!r} is not semver MAJOR.MINOR.PATCH; "
            "the canonical pointer orders own rows by version and cannot rank "
            "a free-form build string"
        )
    pre = match.group("pre")
    if pre is None:
        return (
            int(match.group("major")), int(match.group("minor")),
            int(match.group("patch")), 1, (),
        )
    parts: list[Any] = [
        (0, int(token), "") if token.isdigit() else (1, 0, token)
        for token in pre.split(".")
    ]
    return (
        int(match.group("major")), int(match.group("minor")),
        int(match.group("patch")), 0, tuple(parts),
    )


__all__ = [
    "LANES",
    "LANE_STATUSES",
    "OWN_BACKEND_PREFIX",
    "PRODUCERS",
    "Lane",
    "LaneContractError",
    "LaneResult",
    "LaneStatus",
    "OwnBackend",
    "Producer",
    "SemverError",
    "own_backend",
    "parse_own_backend",
    "semver_key",
    "validate_lane_payload",
    "validate_lane_result",
]
