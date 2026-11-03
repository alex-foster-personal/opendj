"""Host-aware analysis worker concurrency (NATIVE-32).

Measured Wed 7 Oct 2026: a first-import backfill ran four parallel beat_this
workers at about 650 MB each and swapped badly on a 16 GB Mac. The duration
band in :mod:`apps.analysis.admission` alone always picked four workers for
short tracks, with no regard for free RAM or CPU count.

Concurrency is the minimum of:

* floor(available_mb / ram_budget)
* logical CPU count
* the duration-band cap from :func:`apps.analysis.admission.workers_for_longest`
  when one is supplied

``ram_budget`` is the greater of ``concurrency.per_worker_budget_mb`` (the
650 MB short-track floor in ``apps/analysis/config.yaml``) and the admitted
batch's predicted per-worker peak. A flat 650 MB divisor lets a long track
admit more workers than its peak fits. An available-memory budget that
cannot fit even one worker is an explicit error, never a silent fall-back
to zero or one.

-Cursor
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from apps.analysis.config import load_config

log = logging.getLogger("apps.analysis.worker_sizing")

#: Measured Wed 7 Oct 2026: beat_this workers on short tracks ~650 MB RSS each.
#: Kept as the module default so a missing config key fails loudly rather than
#: inventing a different number.
DEFAULT_PER_WORKER_BUDGET_MB: float = 650.0


class InsufficientMemoryError(RuntimeError):
    """Available memory cannot fit even one analysis worker at the budget."""


@dataclass(frozen=True)
class HostCapacity:
    """Live host facts the sizing function budgets against."""

    available_mb: float
    cpu_count: int


@dataclass(frozen=True)
class WorkerSizing:
    """Chosen concurrency plus the reason an operator can read in the log."""

    workers: int
    reason: str
    available_mb: float
    cpu_count: int
    per_worker_budget_mb: float
    duration_cap: int | None
    by_ram: int
    by_cpu: int


def per_worker_budget_mb_from_config(config: dict[str, Any] | None = None) -> float:
    """The configured per-worker memory budget in MB.

    Reads ``concurrency.per_worker_budget_mb`` from analysis config. A missing
    or non-positive value is an explicit error: never invent a budget.
    """
    raw = load_config() if config is None else config
    section = raw.get("concurrency")
    if not isinstance(section, dict):
        raise TypeError(
            "apps/analysis/config.yaml must define concurrency.per_worker_budget_mb; "
            f"got concurrency={section!r}"
        )
    value = section.get("per_worker_budget_mb")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(
            "concurrency.per_worker_budget_mb must be a positive number of MB; "
            f"got {value!r}"
        )
    budget = float(value)
    if budget <= 0.0:
        raise ValueError(
            f"concurrency.per_worker_budget_mb must be positive; got {budget}"
        )
    return budget


def read_host_capacity() -> HostCapacity:
    """Available memory and logical CPU count from psutil. Fail loud on miss."""
    import psutil

    try:
        available = psutil.virtual_memory().available
    except Exception as exc:
        raise RuntimeError(f"psutil.virtual_memory failed: {exc}") from exc
    if not isinstance(available, (int, float)) or available < 0:
        raise RuntimeError(f"psutil.virtual_memory().available invalid: {available!r}")
    try:
        cpus = psutil.cpu_count(logical=True)
    except Exception as exc:
        raise RuntimeError(f"psutil.cpu_count failed: {exc}") from exc
    if cpus is None or not isinstance(cpus, int) or cpus < 1:
        raise RuntimeError(f"psutil.cpu_count returned invalid: {cpus!r}")
    return HostCapacity(available_mb=float(available) / (1024.0 * 1024.0), cpu_count=cpus)


def _effective_ram_budget_mb(
    per_worker_budget_mb: float, predicted_peak_mb: float | None
) -> float:
    """Greater of the configured floor and the batch's predicted peak."""
    if predicted_peak_mb is None:
        return per_worker_budget_mb
    if isinstance(predicted_peak_mb, bool) or not isinstance(
        predicted_peak_mb, (int, float)
    ):
        raise TypeError(
            "predicted_peak_mb must be a positive number of MB when set; "
            f"got {predicted_peak_mb!r}"
        )
    peak = float(predicted_peak_mb)
    if not math.isfinite(peak) or peak <= 0.0:
        raise ValueError(
            "predicted_peak_mb must be a finite positive number of MB when set; "
            f"got {predicted_peak_mb!r}"
        )
    return max(per_worker_budget_mb, peak)


def size_workers(
    *,
    available_mb: float,
    cpu_count: int,
    per_worker_budget_mb: float,
    duration_cap: int | None = None,
    predicted_peak_mb: float | None = None,
) -> WorkerSizing:
    """Pick analysis concurrency from RAM, CPU, and an optional duration cap.

    Pure function of its arguments so tests inject low-memory cases without
    touching the live host. When ``predicted_peak_mb`` exceeds the configured
    floor, that peak is the RAM divisor, so a long track cannot be sized as
    if it cost the short-track floor. Raises :class:`InsufficientMemoryError`
    when not even one worker fits the budget.
    """
    if per_worker_budget_mb <= 0.0:
        raise ValueError(
            f"per_worker_budget_mb must be positive; got {per_worker_budget_mb}"
        )
    if cpu_count < 1:
        raise ValueError(f"cpu_count must be >= 1; got {cpu_count}")
    if available_mb < 0.0:
        raise ValueError(f"available_mb must be >= 0; got {available_mb}")
    if duration_cap is not None and duration_cap < 1:
        raise ValueError(
            f"duration_cap must be >= 1 when set; got {duration_cap}"
        )
    ram_budget = _effective_ram_budget_mb(per_worker_budget_mb, predicted_peak_mb)

    by_ram = int(available_mb // ram_budget)
    if by_ram < 1:
        detail = f"{ram_budget:.0f} MB per worker"
        if predicted_peak_mb is not None and float(predicted_peak_mb) > per_worker_budget_mb:
            detail += (
                f" (duration peak {float(predicted_peak_mb):.0f} MB, "
                f"above the configured floor of {per_worker_budget_mb:.0f} MB)"
            )
        raise InsufficientMemoryError(
            f"available memory {available_mb:.0f} MB cannot fit even one "
            f"analysis worker at {detail}"
        )
    by_cpu = cpu_count
    workers = min(by_ram, by_cpu)
    ram_reason = (
        f"{by_ram} by RAM ({available_mb:.0f} MB available / "
        f"{ram_budget:.0f} MB per worker"
    )
    if predicted_peak_mb is not None and float(predicted_peak_mb) > per_worker_budget_mb:
        ram_reason += f", duration peak {float(predicted_peak_mb):.0f} MB"
    ram_reason += ")"
    parts = [
        ram_reason,
        f"{by_cpu} by CPU",
    ]
    if duration_cap is not None:
        workers = min(workers, duration_cap)
        parts.append(f"{duration_cap} by duration band")
    reason = f"chose {workers} worker(s): min({', '.join(parts)})"
    return WorkerSizing(
        workers=workers,
        reason=reason,
        available_mb=available_mb,
        cpu_count=cpu_count,
        per_worker_budget_mb=ram_budget,
        duration_cap=duration_cap,
        by_ram=by_ram,
        by_cpu=by_cpu,
    )


def size_workers_for_host(
    *,
    duration_cap: int | None = None,
    host: HostCapacity | None = None,
    per_worker_budget_mb: float | None = None,
    predicted_peak_mb: float | None = None,
) -> WorkerSizing:
    """Size workers against live (or injected) host capacity and config budget.

    ``predicted_peak_mb`` raises the RAM divisor when the admitted batch's
    length-model peak exceeds the configured short-track floor.
    """
    capacity = host if host is not None else read_host_capacity()
    budget = (
        per_worker_budget_mb
        if per_worker_budget_mb is not None
        else per_worker_budget_mb_from_config()
    )
    sizing = size_workers(
        available_mb=capacity.available_mb,
        cpu_count=capacity.cpu_count,
        per_worker_budget_mb=budget,
        duration_cap=duration_cap,
        predicted_peak_mb=predicted_peak_mb,
    )
    log.info("analysis concurrency: %s", sizing.reason)
    return sizing


__all__ = [
    "DEFAULT_PER_WORKER_BUDGET_MB",
    "HostCapacity",
    "InsufficientMemoryError",
    "WorkerSizing",
    "per_worker_budget_mb_from_config",
    "read_host_capacity",
    "size_workers",
    "size_workers_for_host",
]
