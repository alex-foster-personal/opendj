"""Shared transfer parameter validation for CLI and service layers."""

from __future__ import annotations

import math

CONFIDENCE_THRESHOLD_MESSAGE = "confidence threshold must be finite and within (0, 1]"
DURATION_TOLERANCE_MESSAGE = "duration tolerance must be a positive integer"


def is_strict_positive_int(value: object) -> bool:
    """True for int values strictly greater than zero, excluding bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def confidence_threshold_error(value: float) -> str | None:
    if not math.isfinite(value) or not 0 < value <= 1:
        return CONFIDENCE_THRESHOLD_MESSAGE
    return None


def duration_tolerance_ms_error(value: int) -> str | None:
    if not is_strict_positive_int(value):
        return DURATION_TOLERANCE_MESSAGE
    return None
