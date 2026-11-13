"""Metric samples from the browser, the Electron shell and the Rust engine (OBS-12).

Every process that is not this one reports latency and counts HERE, so one
aggregator, one scrubber and one consent gate govern every
metric the app could send. The browser ships no Sentry code (OBS-06), the
shell has none, and the Rust engine has no network path of its own; each
posts batches of samples and this route folds them into
:data:`apps.shared.telemetry.metrics.METRICS` with an ``origin`` tag.

``GET`` returns the current, unflushed window, which is how an agent or the
coverage report checks that a surface is actually emitting.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from apps.shared.telemetry.metrics import METRICS, NAME_RE

router = APIRouter(prefix="/telemetry/metrics", tags=["telemetry-metrics"])
MOUNT_UNDER_API_PREFIX = True

MAX_SAMPLES_PER_BATCH = 500


class MetricSampleIn(BaseModel):
    """One sample: a metric name, an optional duration, label tags, an error flag."""

    name: str = Field(min_length=1, max_length=80)
    value_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    tags: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=8)
    error: bool = False


class MetricBatchIn(BaseModel):
    """One posted batch from one origin, at most MAX_SAMPLES_PER_BATCH samples."""

    origin: Literal["browser", "shell", "rust-engine"]
    samples: list[MetricSampleIn] = Field(max_length=MAX_SAMPLES_PER_BATCH)


class MetricBatchOut(BaseModel):
    """How many samples of the batch were recorded and how many refused."""

    accepted: int
    rejected: int


@router.post("", response_model=MetricBatchOut, status_code=202)
def ingest_metric_samples(batch: MetricBatchIn) -> MetricBatchOut:
    """Fold one batch into the aggregator. Unknown tag keys are dropped there."""
    accepted = rejected = 0
    for sample in batch.samples:
        if not NAME_RE.match(sample.name):
            rejected += 1
            continue
        tags: dict[str, Any] = {**sample.tags, "origin": batch.origin}
        METRICS.record(sample.name, sample.value_ms, tags=tags, error=sample.error)
        accepted += 1
    return MetricBatchOut(accepted=accepted, rejected=rejected)


@router.get("")
def current_metric_window() -> dict[str, Any]:
    """The unflushed window plus where summaries go. Read-only, local only."""
    return {
        "series": METRICS.snapshot(),
        "local_log_dir": str(METRICS.log_dir) if METRICS.log_dir else None,
        "forwarding": METRICS.forwarder is not None,
        "rejected_names": METRICS.rejected_names,
        "dropped_pending": METRICS.dropped_pending,
    }
