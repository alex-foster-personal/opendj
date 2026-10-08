"""The memory admission rule: what the backfill queue may run, and how wide.

Spec `(private path)` section 4 ("Runs locally, sharpened"),
requirement NATIVE-10, lane brief `(private path)`
item 2. Host-aware concurrency cap: NATIVE-32.

Peak RSS for a native-analysis producer is LENGTH-DEPENDENT. The round-1
measurement in the spec refuted the earlier "chunking bounds peak memory"
assumption: chunking bounds what the transformer sees at once, not what the
process holds, because the decode, the full-length log-mel spectrogram and
the per-chunk predictions accumulated before aggregation all scale with the
track. Measured on the maintainer's Mac, CPU, one synthetic click track per arm:
396.8 MB peak RSS at 2 minutes and 1617.0 MB at 20 minutes, which is about
68 MB per audio minute above a ~330 MB floor.

So the queue budgets, per track:

    predicted peak = floor_mb + slope_mb_per_min x audio-minutes

against a per-worker cap of 6.5 GB, and the DURATION BAND caps concurrency
from the LONGEST ADMITTED track in the batch:

    every track under 20 minutes      -> 4 workers  (about 6.5 GB total)
    longest 20 to 45 minutes          -> 2 workers  (about 6.9 GB at 45 min)
    longest above 45 minutes          -> 1 worker
    a single track above 90 minutes   -> REFUSED, with a named reason

NATIVE-32 (Wed 7 Oct 2026): that duration band is an UPPER bound, not the
final width. Final concurrency is
``min(duration_band, floor(available_mb / ram_budget), cpu_count)``
via :mod:`apps.analysis.worker_sizing`. ``ram_budget`` is the greater of
the configured short-track floor and this batch's predicted per-worker
peak, because four beat_this workers at ~650 MB each swapped a 16 GB Mac
on first import and a flat 650 MB budget lets a long track overshoot the
same way. GPU presence is still never consulted (nav1-queue brief: the key
producer is CPU by design). The duration-band helper itself stays a pure
function of length; host facts are composed at :func:`admit` time.

It has no hidden default numbers. Every plan carries the
:class:`MemoryModel` it budgeted with, including where that model's
numbers were measured, so a re-measured floor/slope for a new producer
version is distinguishable from the Tue 8 Sep 2026 beat-this figures
rather than silently replacing them.

-Claude
-Cursor
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from apps.analysis.worker_sizing import (
    HostCapacity,
    WorkerSizing,
    size_workers_for_host,
)

#: Per-worker peak RSS cap. 6.5 GB, expressed in MB so it compares directly
#: against a predicted peak. Spec section 4 states the cap the section 8
#: budget line assumes.
PER_WORKER_CAP_MB: float = 6.5 * 1024.0

#: A track longer than this is refused outright rather than downgraded to a
#: single worker (spec section 4: "a track above 90 minutes ... is refused
#: with a named reason rather than swapped").
MAX_ADMITTED_MINUTES: float = 90.0

#: Band boundaries, in minutes, as the spec's complete rule states them.
BAND_UNDER_20_MIN: str = "under_20_min"
BAND_20_TO_45_MIN: str = "20_to_45_min"
BAND_OVER_45_MIN: str = "over_45_min"
#: Not a band the spec names: what an admitted set of size zero reports, so
#: "no work" can never be read as "four workers".
BAND_EMPTY: str = "empty"

#: Refusal reasons. Named constants rather than free text: the queue stores
#: them, the HTTP progress payload serializes them and the CLI prints them,
#: so a refusal is greppable end to end.
REFUSED_LONGER_THAN_MAX: str = "track_longer_than_max_admitted_minutes"
REFUSED_PEAK_OVER_WORKER_CAP: str = "predicted_peak_over_worker_cap"
REFUSED_DURATION_UNKNOWN: str = "duration_unknown"


#-----------------------------------------------------------------------------
# memory model
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryModel:
    """A producer's measured peak-RSS model, with its provenance.

    ``floor_mb`` and ``slope_mb_per_min`` are MEASURED numbers, not
    constants of the universe: a new producer version re-measures them and
    registers its own model. ``measured_on`` and ``source`` exist so a
    report can say which measurement it budgeted against instead of
    implying every producer shares beat-this's slope.
    """

    backend: str
    producer_version: str
    floor_mb: float
    slope_mb_per_min: float
    measured_on: str
    source: str


#: The one model measured so far (spec section 4, round 1). Registered for
#: the beat-this backfill producer. It is the DEFAULT only in the sense that
#: an unregistered producer budgets against it and SAYS SO in the plan; it
#: is never silently substituted for a producer's own measurement.
DEFAULT_MEMORY_MODEL: MemoryModel = MemoryModel(
    backend="own_beatgrid.backfill",
    producer_version="1.1.0",
    floor_mb=330.0,
    slope_mb_per_min=68.0,
    measured_on=(
        "the maintainer's Mac, CPU, one synthetic click track per arm, Tue 8 Sep 2026: "
        "396.8 MB peak RSS at 2 min, 1617.0 MB at 20 min"
    ),
    source="(private path) section 4 (round 1)",
)

#: The pre-v1 librosa backfill producer, re-measured by the same two-arm
#: method on Wed 9 Sep 2026. It is here because it is the ONE registered
#: backend a real backfill can run today, and because its numbers are
#: NOTHING like beat-this's: a floor more than twice as high and a slope
#: half as steep. That is the whole reason this registry exists rather than
#: two module constants -- one producer's measurement does not describe
#: another's, and a queue that budgeted every producer at 330 + 68 would be
#: quoting a figure it never measured for the thing it is running.
LIBROSA_MEMORY_MODEL: MemoryModel = MemoryModel(
    backend="librosa",
    producer_version="librosa-0.10",
    floor_mb=765.5,
    slope_mb_per_min=33.2,
    measured_on=(
        "the maintainer's Mac (load average 161), CPU, one synthetic 128 BPM click "
        "track per arm, peak RSS of the analyzing child process sampled at "
        "20 Hz, Wed 9 Sep 2026: 831.9 MB at 2 min, 1428.7 MB at 20 min"
    ),
    source="(private path), nav1-queue round A",
)

#: Registry keyed on backend name (``own_<lane>.<producer>``, or a pre-v1
#: backend name). A producer lane registers its own re-measured model at
#: import time.
MEMORY_MODELS: dict[str, MemoryModel] = {
    DEFAULT_MEMORY_MODEL.backend: DEFAULT_MEMORY_MODEL,
    LIBROSA_MEMORY_MODEL.backend: LIBROSA_MEMORY_MODEL,
}


def register_memory_model(model: MemoryModel) -> None:
    """Register (or replace) the measured memory model for a producer."""
    MEMORY_MODELS[model.backend] = model


def memory_model_for(backend: str) -> MemoryModel:
    """The model to budget ``backend`` with.

    Falls back to :data:`DEFAULT_MEMORY_MODEL` for a producer that has not
    registered its own. That is a stated fall-back, not a hidden one: the
    returned model names the backend it was measured on, and every plan and
    every progress payload carries it, so a report can never claim a
    measurement it does not have.
    """
    return MEMORY_MODELS.get(backend, DEFAULT_MEMORY_MODEL)


def predicted_peak_mb(model: MemoryModel, duration_s: float) -> float:
    """Predicted peak RSS in MB for one track under ``model``."""
    return model.floor_mb + model.slope_mb_per_min * (duration_s / 60.0)


def workers_for_longest(longest_admitted_s: float) -> int:
    """Duration-band UPPER bound for a batch whose longest track is that long.

    Pure function of duration. Host RAM and CPU are applied later by
    :func:`resolve_workers` / :func:`admit` (NATIVE-32). Explicit ``elif``
    per band so a reader sees every state.
    """
    minutes = longest_admitted_s / 60.0
    if minutes < 20.0:
        return 4
    if minutes <= 45.0:
        return 2
    if minutes <= MAX_ADMITTED_MINUTES:
        return 1
    raise ValueError(
        f"{minutes:.2f} min exceeds MAX_ADMITTED_MINUTES={MAX_ADMITTED_MINUTES}; "
        "such a track is refused by admit(), never sized for"
    )


def _positive_mb(value: object) -> float | None:
    """A finite positive MB figure, or None when it cannot size a worker.

    NaN compares as not less than or equal to zero, so a sign check would
    accept it and a later max() against the configured floor would keep
    that floor.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def highest_predicted_peak_mb(peaks: Sequence[float | None]) -> float:
    """Largest positive predicted peak.

    A missing, non-numeric, non-finite, or non-positive value is an error.
    Skipping it would size the batch at the 650 MB floor and start too many
    workers.
    """
    if not peaks:
        raise ValueError(
            "every runnable item needs a positive predicted peak; got no items"
        )
    chosen: float | None = None
    for peak in peaks:
        value = _positive_mb(peak)
        if value is None:
            raise ValueError(
                "every runnable item needs a positive predicted peak; "
                f"got {peak!r}"
            )
        if chosen is None or value > chosen:
            chosen = value
    if chosen is None:
        raise ValueError(
            "every runnable item needs a positive predicted peak; got no items"
        )
    return chosen


def _required_text(raw: dict[str, object], key: str) -> str:
    """A non-blank string field. Missing or coerced values are an error."""
    if key not in raw:
        raise ValueError(
            f"stored memory model field {key} must be a non-blank string; missing"
        )
    value = raw[key]
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(
            f"stored memory model field {key} must be a non-blank string; "
            f"got {value!r}"
        )
    return value


def memory_model_from_stored(raw: object) -> MemoryModel:
    """Rebuild a memory model from the JSON stored on a batch row.

    Backend, producer version, measured_on, and source must already be
    non-blank strings. Empty provenance is not invented.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"stored memory model must be an object; got {raw!r}")
    backend = _required_text(raw, "backend")
    producer_version = _required_text(raw, "producer_version")
    measured_on = _required_text(raw, "measured_on")
    source = _required_text(raw, "source")
    try:
        floor = raw["floor_mb"]
        slope = raw["slope_mb_per_min"]
        if isinstance(floor, bool) or isinstance(slope, bool):
            raise TypeError("floor and slope must be numbers")
        return MemoryModel(
            backend=backend,
            producer_version=producer_version,
            floor_mb=float(floor),
            slope_mb_per_min=float(slope),
            measured_on=measured_on,
            source=source,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"stored memory model cannot recompute a peak: {raw!r}"
        ) from exc


def runnable_budget_peak_mb(
    items: Sequence[tuple[float | None, float | None]],
    stored_model: object,
) -> float:
    """Heaviest peak for runnable items.

    A stored peak that is missing or not positive is recomputed from the
    batch memory model and that item's duration. If it cannot be recomputed,
    this raises rather than falling back to the short-track floor.

    The stored model is validated on every call, including when every peak
    is usable: a stored peak is only as trustworthy as the provenance of the
    model that produced it.
    """
    if not items:
        raise ValueError(
            "every runnable item needs a positive predicted peak; got no items"
        )
    model = memory_model_from_stored(stored_model)
    coerced: list[float] = []
    for peak, duration in items:
        value = _positive_mb(peak)
        if value is None:
            duration_value = _positive_mb(duration)
            if duration_value is None:
                raise ValueError(
                    "runnable item has no positive predicted peak "
                    f"({peak!r}) and no positive duration to recompute one"
                )
            value = predicted_peak_mb(model, duration_value)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"recomputed peak {value} MB is not positive")
        coerced.append(value)
    return highest_predicted_peak_mb(coerced)


def resolve_workers(
    longest_admitted_s: float,
    *,
    host: HostCapacity | None = None,
    per_worker_budget_mb: float | None = None,
    predicted_peak_mb: float | None = None,
) -> WorkerSizing:
    """Final concurrency: duration band capped by available RAM and CPU.

    ``predicted_peak_mb`` is the admitted batch's heaviest predicted peak.
    It raises the RAM divisor above the configured floor when the track is
    long enough that 650 MB would overshoot.
    """
    return size_workers_for_host(
        duration_cap=workers_for_longest(longest_admitted_s),
        host=host,
        per_worker_budget_mb=per_worker_budget_mb,
        predicted_peak_mb=predicted_peak_mb,
    )


def band_for_longest(longest_admitted_s: float) -> str:
    """The band name that goes in the round log beside the wall clock."""
    minutes = longest_admitted_s / 60.0
    if minutes < 20.0:
        return BAND_UNDER_20_MIN
    if minutes <= 45.0:
        return BAND_20_TO_45_MIN
    return BAND_OVER_45_MIN


def duration_band_cap(
    durations_s: Sequence[float | None],
    stored_band: str | None = None,
) -> tuple[int, str]:
    """Duration-band worker cap and the band name that produced it.

    A positive finite remaining duration wins. The admission worker count
    is not a duration cap: it may already be a RAM or CPU limit. The stored
    band is only the fallback when every remaining duration is unusable.
    A mix of usable and unusable durations raises: dropping the bad rows
    would size the batch from the short tracks alone and could widen past
    the stored band.
    """
    usable: list[float] = []
    unusable = 0
    for raw in durations_s:
        value = _positive_mb(raw)
        if value is None:
            unusable += 1
            continue
        usable.append(value)
    if usable and unusable:
        raise ValueError(
            "remaining durations must all be positive and finite, or all "
            f"unusable; got {unusable} unusable of {len(durations_s)}"
        )
    if usable:
        longest = max(usable)
        return workers_for_longest(longest), band_for_longest(longest)
    if stored_band == BAND_UNDER_20_MIN:
        return workers_for_longest(60.0), BAND_UNDER_20_MIN
    if stored_band == BAND_20_TO_45_MIN:
        return workers_for_longest(20.0 * 60.0), BAND_20_TO_45_MIN
    if stored_band == BAND_OVER_45_MIN:
        return workers_for_longest(46.0 * 60.0), BAND_OVER_45_MIN
    raise ValueError(
        "cannot derive a duration-band cap from remaining durations "
        f"or stored band {stored_band!r}"
    )


#-----------------------------------------------------------------------------
# admission
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """A track offered to the queue for one lane."""

    stable_id: str
    lane: str
    backend: str
    file_path: str
    duration_s: float | None


@dataclass(frozen=True)
class Admitted:
    """A candidate the rule accepted, with the number it was budgeted at."""

    stable_id: str
    lane: str
    backend: str
    file_path: str
    duration_s: float
    predicted_peak_mb: float


@dataclass(frozen=True)
class Refused:
    """A candidate the rule refused, and why, in words an operator can act on."""

    stable_id: str
    lane: str
    backend: str
    reason: str
    detail: str
    duration_s: float | None
    predicted_peak_mb: float | None


@dataclass(frozen=True)
class AdmissionPlan:
    """What the queue will run, how wide, and what it will not run."""

    admitted: list[Admitted]
    refused: list[Refused]
    workers: int
    band: str
    longest_admitted_s: float | None
    model: MemoryModel
    workers_reason: str

    def __post_init__(self) -> None:
        reason = self.workers_reason
        if not isinstance(reason, str) or reason.strip() == "":
            raise ValueError(
                "AdmissionPlan.workers_reason is required and must be "
                "non-blank; every plan carries why its worker count was chosen"
            )


def admit(
    candidates: Sequence[Candidate],
    *,
    model: MemoryModel,
    host: HostCapacity | None = None,
    per_worker_budget_mb: float | None = None,
) -> AdmissionPlan:
    """Apply the memory admission rule to a batch.

    Refusals are per track and named; concurrency is a property of the
    ADMITTED set, so refusing one 3 hour bootleg does not drag a library of
    club edits down to one worker. Final worker count is the duration band
    capped by available RAM and CPU (NATIVE-32).
    """
    admitted: list[Admitted] = []
    refused: list[Refused] = []
    for cand in candidates:
        duration = cand.duration_s
        if duration is None or duration <= 0.0:
            refused.append(
                Refused(
                    stable_id=cand.stable_id,
                    lane=cand.lane,
                    backend=cand.backend,
                    reason=REFUSED_DURATION_UNKNOWN,
                    detail=(
                        "no positive duration is known for this track, so its "
                        "peak memory cannot be predicted; the queue refuses "
                        "rather than guessing a length"
                    ),
                    duration_s=duration,
                    predicted_peak_mb=None,
                )
            )
            continue
        minutes = duration / 60.0
        peak = predicted_peak_mb(model, duration)
        if minutes > MAX_ADMITTED_MINUTES:
            refused.append(
                Refused(
                    stable_id=cand.stable_id,
                    lane=cand.lane,
                    backend=cand.backend,
                    reason=REFUSED_LONGER_THAN_MAX,
                    detail=(
                        f"{minutes:.1f} min exceeds the "
                        f"{MAX_ADMITTED_MINUTES:.0f} min ceiling "
                        f"(predicted peak {peak:.0f} MB)"
                    ),
                    duration_s=duration,
                    predicted_peak_mb=peak,
                )
            )
            continue
        if peak > PER_WORKER_CAP_MB:
            refused.append(
                Refused(
                    stable_id=cand.stable_id,
                    lane=cand.lane,
                    backend=cand.backend,
                    reason=REFUSED_PEAK_OVER_WORKER_CAP,
                    detail=(
                        f"predicted peak {peak:.0f} MB exceeds the per-worker "
                        f"cap of {PER_WORKER_CAP_MB:.0f} MB under model "
                        f"{model.backend}@{model.producer_version}"
                    ),
                    duration_s=duration,
                    predicted_peak_mb=peak,
                )
            )
            continue
        admitted.append(
            Admitted(
                stable_id=cand.stable_id,
                lane=cand.lane,
                backend=cand.backend,
                file_path=cand.file_path,
                duration_s=duration,
                predicted_peak_mb=peak,
            )
        )

    if not admitted:
        return AdmissionPlan(
            admitted=[],
            refused=refused,
            workers=0,
            band=BAND_EMPTY,
            longest_admitted_s=None,
            model=model,
            workers_reason="empty admitted set: zero workers",
        )
    longest = max(item.duration_s for item in admitted)
    peak = highest_predicted_peak_mb(
        [item.predicted_peak_mb for item in admitted]
    )
    sizing = resolve_workers(
        longest,
        host=host,
        per_worker_budget_mb=per_worker_budget_mb,
        predicted_peak_mb=peak,
    )
    return AdmissionPlan(
        admitted=admitted,
        refused=refused,
        workers=sizing.workers,
        band=band_for_longest(longest),
        longest_admitted_s=longest,
        model=model,
        workers_reason=sizing.reason,
    )


__all__ = [
    "BAND_20_TO_45_MIN",
    "BAND_EMPTY",
    "BAND_OVER_45_MIN",
    "BAND_UNDER_20_MIN",
    "DEFAULT_MEMORY_MODEL",
    "LIBROSA_MEMORY_MODEL",
    "MAX_ADMITTED_MINUTES",
    "MEMORY_MODELS",
    "PER_WORKER_CAP_MB",
    "REFUSED_DURATION_UNKNOWN",
    "REFUSED_LONGER_THAN_MAX",
    "REFUSED_PEAK_OVER_WORKER_CAP",
    "AdmissionPlan",
    "Admitted",
    "Candidate",
    "MemoryModel",
    "Refused",
    "admit",
    "band_for_longest",
    "memory_model_for",
    "predicted_peak_mb",
    "highest_predicted_peak_mb",
    "memory_model_from_stored",
    "register_memory_model",
    "runnable_budget_peak_mb",
    "resolve_workers",
    "workers_for_longest",
]
