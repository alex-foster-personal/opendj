"""Classify demucs worker JSON as completed separation vs unavailable.

Hermetic callers and ``_process_one`` use this before vocal-cache publication so
a payload that only looks like ``regions: []`` cannot masquerade as a finished run
when demucs never executed. Stdlib only; torch/demucs stay in the PEP 723 worker.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from apps.vocals import cache as vcache

MODEL_NAME: str = "htdemucs"


class DemucsUnavailableError(RuntimeError):
    """Demucs analysis did not run; not the same as completed with zero regions."""


@dataclass(frozen=True)
class DemucsAnalysisOutcome:
    kind: Literal["completed", "unavailable"]
    payload: dict[str, Any] | None = None
    reason: str | None = None
    error_type: str | None = None


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _has_separation_evidence(payload: Mapping[str, Any]) -> bool:
    device = payload.get("device")
    if not isinstance(device, str) or not device.strip():
        return False
    timings = payload.get("timings")
    if not isinstance(timings, Mapping):
        return False
    separate_s = timings.get("separate_s")
    if not _is_finite_number(separate_s) or float(separate_s) <= 0:
        return False
    for key in ("source_sample_rate", "analysis_sample_rate"):
        rate = payload.get(key)
        if not _is_finite_number(rate) or float(rate) <= 0:
            return False
    params = payload.get("params")
    if not isinstance(params, Mapping):
        return False
    return params.get("model") == MODEL_NAME


def _satisfies_cache_worker_shape(payload: Mapping[str, Any]) -> bool:
    missing = [field for field in vcache._REQUIRED_WORKER_FIELDS if field not in payload]
    if missing:
        return False
    if payload.get("schema") != vcache.VOCAL_CACHE_SCHEMA:
        return False
    return payload.get("source") == vcache.VOCAL_CACHE_SOURCE


def classify_worker_result(payload: Mapping[str, Any]) -> DemucsAnalysisOutcome:
    """Return completed vs unavailable from a worker dict (no subprocess)."""
    if _satisfies_cache_worker_shape(payload) and _has_separation_evidence(payload):
        return DemucsAnalysisOutcome(
            kind="completed",
            payload=dict(payload),
        )
    return DemucsAnalysisOutcome(
        kind="unavailable",
        reason="worker result lacks demucs separation evidence",
    )


def outcome_from_exception(exc: BaseException) -> DemucsAnalysisOutcome:
    """Map worker and publication failures to an unavailable outcome."""
    return DemucsAnalysisOutcome(
        kind="unavailable",
        reason=str(exc) or type(exc).__name__,
        error_type=type(exc).__name__,
    )


def assert_publishable_worker_result(payload: Mapping[str, Any]) -> None:
    """Raise DemucsUnavailableError if payload is not proof of a finished separation."""
    outcome = classify_worker_result(payload)
    if outcome.kind != "completed":
        raise DemucsUnavailableError(outcome.reason or "demucs worker result is not publishable")


def is_empty_but_completed(payload: Mapping[str, Any]) -> bool:
    """True when regions is empty but separation evidence is present."""
    regions = payload.get("regions")
    if not isinstance(regions, list) or regions:
        return False
    outcome = classify_worker_result(payload)
    return outcome.kind == "completed"


__all__ = [
    "MODEL_NAME",
    "DemucsAnalysisOutcome",
    "DemucsUnavailableError",
    "assert_publishable_worker_result",
    "classify_worker_result",
    "is_empty_but_completed",
    "outcome_from_exception",
]
