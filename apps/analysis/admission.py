"""The memory admission rule: what the backfill queue may run, and how wide.

Spec `specs/native-analysis-v1.md` section 4 ("Runs locally, sharpened"),
requirement NATIVE-10, lane brief `specs/native-analysis-v1-lanes/nav1-queue.md`
item 2.

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

against a per-worker cap of 6.5 GB, and it picks concurrency from the
LONGEST ADMITTED track in the batch, never from the core count:

    every track under 20 minutes      -> 4 workers  (about 6.5 GB total)
    longest 20 to 45 minutes          -> 2 workers  (about 6.9 GB at 45 min)
    longest above 45 minutes          -> 1 worker
    a single track above 90 minutes   -> REFUSED, with a named reason

Two properties this module deliberately does NOT have:

* It never consults core count, GPU presence or free RAM. Core count is
  what the spec forbids. GPU presence is what the nav1-queue brief forbids:
  the key producer runs on CPU by design, so "no GPU" must never become
  "refuse on a resource basis". A test asserts `cpu_count` does not appear
  in this module's source.
* It has no hidden default numbers. Every plan carries the
  :class:`MemoryModel` it budgeted with, including where that model's
  numbers were measured, so a re-measured floor/slope for a new producer
  version is distinguishable from the Tue 8 Sep 2026 beat-this figures
  rather than silently replacing them.

-Claude
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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
    source="specs/native-analysis-v1.md section 4 (round 1)",
)

#: Registry keyed on backend name (``own_<lane>.<producer>``). A producer
#: lane registers its own re-measured model at import time.
MEMORY_MODELS: dict[str, MemoryModel] = {
    DEFAULT_MEMORY_MODEL.backend: DEFAULT_MEMORY_MODEL,
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
    """Concurrency for a batch whose longest admitted track is that long.

    Explicit ``elif`` per band so a reader sees every state, and never a
    core count.
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


def band_for_longest(longest_admitted_s: float) -> str:
    """The band name that goes in the round log beside the wall clock."""
    minutes = longest_admitted_s / 60.0
    if minutes < 20.0:
        return BAND_UNDER_20_MIN
    if minutes <= 45.0:
        return BAND_20_TO_45_MIN
    return BAND_OVER_45_MIN


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


def admit(
    candidates: Sequence[Candidate], *, model: MemoryModel
) -> AdmissionPlan:
    """Apply the memory admission rule to a batch.

    Refusals are per track and named; concurrency is a property of the
    ADMITTED set, so refusing one 3 hour bootleg does not drag a library of
    club edits down to one worker.
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
        )
    longest = max(item.duration_s for item in admitted)
    return AdmissionPlan(
        admitted=admitted,
        refused=refused,
        workers=workers_for_longest(longest),
        band=band_for_longest(longest),
        longest_admitted_s=longest,
        model=model,
    )


__all__ = [
    "BAND_20_TO_45_MIN",
    "BAND_EMPTY",
    "BAND_OVER_45_MIN",
    "BAND_UNDER_20_MIN",
    "DEFAULT_MEMORY_MODEL",
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
    "register_memory_model",
    "workers_for_longest",
]
