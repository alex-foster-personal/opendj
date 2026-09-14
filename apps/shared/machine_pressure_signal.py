"""Shared PERFMODE-04 pressure-elevated predicate (no webui dependency).

The sampler and cache live in ``apps.webui.server.machine_pressure``; this
module holds only the locked threshold logic so ``apps.shared`` and domain
packages can gate on an already-read payload without importing the delivery
layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Locked signals (specs/perf-latency-program.md Pressure cluster, PERFMODE-05).
KERNEL_ELEVATED_LEVEL = 2
PRESSURE_CHURN_EARLY_WARNING = 500


def pressure_is_elevated(payload: Mapping[str, Any]) -> bool:
    """True on locked PERFMODE-04 signals. False when available is false or fields absent."""
    if payload.get("available") is False:
        return False
    return _is_elevated(dict(payload))


def _is_elevated(values: dict[str, Any]) -> bool:
    kernel = values.get("kernel_memory_pressure_level")
    if isinstance(kernel, int) and kernel >= KERNEL_ELEVATED_LEVEL:
        return True
    churn = values.get("churn_score")
    return isinstance(churn, (int, float)) and churn >= PRESSURE_CHURN_EARLY_WARNING


__all__ = [
    "KERNEL_ELEVATED_LEVEL",
    "PRESSURE_CHURN_EARLY_WARNING",
    "pressure_is_elevated",
]
