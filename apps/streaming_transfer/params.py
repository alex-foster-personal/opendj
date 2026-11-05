"""Shared transfer parameter validation for CLI and service layers."""

from __future__ import annotations

import math

CONFIDENCE_THRESHOLD_MESSAGE = "confidence threshold must be finite and within (0, 1]"
DURATION_TOLERANCE_MESSAGE = "duration tolerance must be a positive integer"


def confidence_threshold_error(value: float) -> str | None:
    if not math.isfinite(value) or not 0 < value <= 1:
        return CONFIDENCE_THRESHOLD_MESSAGE
    return None


def duration_tolerance_ms_error(value: int) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return DURATION_TOLERANCE_MESSAGE
    return None
