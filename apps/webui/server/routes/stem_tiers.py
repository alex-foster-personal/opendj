"""Separation-tier catalogue and per-track time/cost estimates.

Read-only. Runs nothing, spends nothing, and never guesses: every number comes
from :mod:`apps.stems.tiers`, which will raise rather than interpolate an
estimate for a (tier, GPU) pair that has not been benchmarked.

Mounted at ``/api/v1`` by the application integrator.

  GET /api/v1/stems/tiers                  the three products and their evidence
  GET /api/v1/stems/estimate?seconds=240   all three tiers costed for one track
  GET /api/v1/stems/estimate/batch?...     wall clock for a whole batch

AGENT-NATIVE PARITY: these mirror ``python -m apps.stems estimate`` exactly, so
an agent can answer the same question the UI does without driving a browser.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from apps.stems import tiers as tiercfg

router = APIRouter(prefix="/stems", tags=["stem-tiers"])


class TierOut(BaseModel):
    """One separation product, with the evidence behind the choice."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    preset_tag: str
    model: str
    overlap: float
    shifts: int
    gpu: str
    purpose: str
    evidence: str
    evidence_strength: str
    is_default: bool


class TierEstimateOut(BaseModel):
    """Cost of one track at one tier. ``measured`` false means we do not know."""

    model_config = ConfigDict(frozen=True)

    tier: str
    name: str
    gpu: str
    measured: bool
    seconds: Optional[float] = None
    usd: Optional[float] = None
    measured_at: Optional[str] = None
    n_tracks: Optional[int] = None
    r_squared: Optional[float] = None
    unavailable_reason: Optional[str] = None


class EstimateOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    duration_s: float
    tiers: list[TierEstimateOut]


def _ensure_loaded() -> None:
    """Load the measured fits once. Cheap, idempotent, and explicit."""
    if not tiercfg.THROUGHPUT:
        from apps.shared.paths import DATA_DIR

        tiercfg.load_measured_throughput(DATA_DIR.parent)


@router.get("/tiers", response_model=list[TierOut])
def list_tiers() -> list[TierOut]:
    return [
        TierOut(
            key=t.key,
            name=t.name,
            preset_tag=t.preset_tag,
            model=t.model,
            overlap=t.overlap,
            shifts=t.shifts,
            gpu=t.gpu,
            purpose=t.purpose,
            evidence=t.evidence,
            evidence_strength=t.evidence_strength,
            is_default=(t.key == tiercfg.DEFAULT_TIER),
        )
        for t in tiercfg.TIERS.values()
    ]


@router.get("/estimate", response_model=EstimateOut)
def estimate(
    seconds: float = Query(..., gt=0, description="track duration in seconds"),
    gpu: Optional[str] = Query(None, description="override the card"),
) -> EstimateOut:
    """Every tier costed for one track, so a UI needs one round trip not three.

    A tier with no benchmark returns ``measured: false`` and the reason rather
    than a plausible-looking guess. Rendering a guess as a number is how an
    assumption becomes ground truth.
    """
    _ensure_loaded()
    out: list[TierEstimateOut] = []
    for tier in tiercfg.TIERS.values():
        card = gpu or tier.gpu
        try:
            secs = tiercfg.estimate_seconds(seconds, tier.key, card)
            usd = tiercfg.estimate_usd(seconds, tier.key, card)
            tp = tiercfg.THROUGHPUT[f"{tier.key}@{card}"]
            out.append(TierEstimateOut(
                tier=tier.key, name=tier.name, gpu=card, measured=True,
                seconds=round(secs, 1), usd=round(usd, 4),
                measured_at=tp.measured_at, n_tracks=tp.n_tracks,
                r_squared=tp.r_squared,
            ))
        except tiercfg.ThroughputNotMeasured as exc:
            out.append(TierEstimateOut(
                tier=tier.key, name=tier.name, gpu=card, measured=False,
                unavailable_reason=str(exc),
            ))
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return EstimateOut(duration_s=seconds, tiers=out)


@router.get("/estimate/batch", response_model=dict)
def estimate_batch(
    seconds: list[float] = Query(..., description="repeat once per track"),
    tier: str = Query(tiercfg.DEFAULT_TIER),
    gpu: Optional[str] = Query(None),
) -> dict:
    """Wall clock for a whole batch, packed longest-first across GPU slots.

    GPU-SIDE ONLY. It does not model the upload feeder, which has been the
    binding constraint on real runs from this Mac. A floor, not a promise.
    """
    _ensure_loaded()
    try:
        total = tiercfg.estimate_batch_seconds(list(seconds), tier, gpu)
    except tiercfg.ThroughputNotMeasured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "tier": tier.upper(),
        "gpu": gpu or tiercfg.get_tier(tier).gpu,
        "n_tracks": len(seconds),
        "total_audio_s": round(sum(seconds), 1),
        "max_concurrent_gpus": tiercfg.MAX_CONCURRENT_GPUS,
        "wall_s": round(total, 1),
        "wall_human": f"{int(total // 60)}m {int(total % 60)}s",
        "caveat": (
            "GPU-side only; excludes the local upload feeder, which has been "
            "the real binding constraint. Treat as a floor."
        ),
    }
