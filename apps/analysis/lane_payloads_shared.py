"""Generic payload-value primitives shared across lane validators.

A leaf module (only depends on :mod:`apps.analysis.lane_enums`), for the same
reason that module exists: :mod:`apps.analysis.lane_payloads` split out its
own beatgrid/key/waveform/loudness/vocal sections once it neared the
600-line limit again, and each of those sections needs these checks without
re-deriving them or creating an import cycle between the split-out halves.

-Claude
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .lane_enums import LaneContractError


def is_finite_number(value: int | float) -> bool:
    """`math.isfinite` that answers False for an int too large for a C double.

    `math.isfinite(10**400)` raises `OverflowError` rather than returning
    False, so a JSON integer of that size escaped every finiteness gate as
    an uncaught exception instead of a contract error (Codex P2, PR #1562).
    """
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


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
    if not is_finite_number(value):
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


__all__ = [
    "_require_bool",
    "_require_confidence",
    "_require_keys",
    "_require_list",
    "_require_number",
    "_require_positive",
    "_require_str",
]
