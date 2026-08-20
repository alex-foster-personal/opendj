"""Stems planning HTTP surface: what a stems run would cost, before it runs.

The install flow asks the tester one question -- "separate stems for your
library?" -- and a question like that is unanswerable without three numbers:
how many tracks, how long, how much of the maintainer's money. This endpoint is where
those numbers come from, and it is the SAME arithmetic the job itself uses,
so the prompt cannot promise a batch the run then disagrees with.

Agent-native parity: the prompt has no private knowledge. Everything the
wizard renders is here, so an agent can read the plan, decide, and enqueue
through ``POST /api/v1/jobs`` without driving a single pixel.

Mounted by the ENGINE only. Separating stems means enqueueing an engine job,
so offering the plan on a legacy boot would advertise a button that boot
cannot press.

Requirements (mini-PRD):
  ✔︎ ✅ the plan names its denominator, never just a count.
    [if] 900 rows have no audio [then] the response says so in its own field
  ✔︎ ✅ an unmeasured tier/card pair refuses instead of interpolating.
    [if] throughput was never measured for that pair [then ⛔️] 503, not a
      made-up number

-Claude
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from apps.stems.selection import library_buckets
from apps.stems.tiers import (
    DEFAULT_TIER,
    ThroughputNotMeasured,
    estimate_batch_seconds,
    estimate_usd,
    modal_tiers,
)

router = APIRouter(prefix="/stems", tags=["stems"])

MODAL_TIER_KEYS: tuple[str, ...] = tuple(tier.key for tier in modal_tiers())

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
"""Where ``scripts/bench/tier_throughput.json`` lives.

From THIS MODULE's location, not from the data dir. The measurements are
repo content and the data dir is wherever the operator pointed ``--data-dir``,
so deriving one from the other only works while they happen to be siblings --
which they are not for any worktree run with its own data dir.
"""


def _ensure_throughput_loaded() -> None:
    """Load the measured fits once. Cheap, idempotent, explicit."""
    from apps.stems import tiers as tiercfg

    if not tiercfg.THROUGHPUT:
        tiercfg.load_measured_throughput(REPO_ROOT)


class StemsPlanOut(BaseModel):
    """What separating this library at this tier would take."""

    tier: str
    tier_name: str
    # THE DENOMINATOR, spelled out. pending + ready + unavailable == total.
    total: int = Field(description="library rows carrying a file path")
    pending: int = Field(description="audio on disk, no bundle yet: the work")
    ready: int = Field(description="already has a stem bundle on disk")
    unavailable: int = Field(description="library row exists, its file does not")
    estimate_seconds: float = Field(
        description="wall-clock floor for the pending batch, GPU side only"
    )
    estimate_usd: float = Field(description="GPU cost of the pending batch")
    # CAN THIS BUILD ACTUALLY RUN IT. The counts above describe work that
    # exists; they say nothing about whether this machine can reach a GPU.
    # Without this the prompt offers a button, the job is accepted, and the
    # worker dies on a missing credential AFTER the wizard has shown "Done".
    transport: str = Field(description="how this build reaches a GPU: relay or direct")
    transport_refusal: str | None = Field(
        default=None,
        description="why a run cannot start on this build; null when it can",
    )


def stems_transport_state() -> tuple[str, str | None]:
    """The build's GPU transport, and why it cannot be used (or None).

    Asks the transport's OWN preflight rather than re-deriving the rules here,
    so the answer the prompt shows and the answer the worker hits are the same
    code. A build with no relay base and no identity token cannot separate
    anything, and saying so up front is the difference between an honest inert
    step and a job that is accepted, queued, and then dies on a credential the
    tester was never given.

    Imported locally: the relay client pulls httpx, and the plan endpoint is on
    the engine boot path.
    """
    from apps.stems.job import (
        DEFAULT_TRANSPORT,
        StemsJobPayloadError,
        resolve_transport,
    )

    try:
        transport = resolve_transport()
    except StemsJobPayloadError as exc:
        # A misconfigured transport env is itself a refusal, and naming it is
        # more useful than pretending the default applies.
        return DEFAULT_TRANSPORT, str(exc)
    if transport != "relay":
        # 'direct' is opt-in through MDT_STEMS_TRANSPORT and means the operator
        # deliberately supplied a Modal credential on this machine. The worker
        # verifies it for real; there is nothing to preflight from here.
        return transport, None
    from apps.stems.relay.client import RelayUnavailable, identity_token, relay_base_url

    try:
        relay_base_url()
        identity_token()
    except RelayUnavailable as exc:
        return transport, str(exc)
    return transport, None


def _data_dir(request: Request) -> Path:
    cfg = getattr(request.app.state, "engine_cfg", None)
    if cfg is None:
        raise RuntimeError("engine config is not mounted on app.state.engine_cfg")
    return Path(cfg.data_dir)


@router.get("/plan", response_model=StemsPlanOut)
def get_stems_plan(
    request: Request,
    tier: Annotated[str, Query(description="Modal rung: S, M or L")] = DEFAULT_TIER,
) -> dict[str, Any]:
    if tier not in MODAL_TIER_KEYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tier {tier!r} is not a Modal tier; known: {list(MODAL_TIER_KEYS)}",
        )
    from apps.stems.tiers import get_tier

    _ensure_throughput_loaded()
    try:
        buckets = library_buckets(_data_dir(request))
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    durations = buckets.pending_durations_s()
    try:
        seconds = estimate_batch_seconds(durations, tier)
        usd = sum(estimate_usd(duration, tier) for duration in durations)
    except ThroughputNotMeasured as exc:
        # An unmeasured pair is a real gap, and a plausible-looking number
        # here would be spent money justified by a guess.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    # The counts are reported even when the transport is refused: "217 tracks
    # would need stems, and this build cannot run them" is a more useful truth
    # than hiding the work behind the refusal.
    transport, transport_refusal = stems_transport_state()

    return {
        "tier": tier,
        "tier_name": get_tier(tier).name,
        "total": buckets.total,
        "pending": len(buckets.pending),
        "ready": len(buckets.ready),
        "unavailable": len(buckets.unavailable),
        "estimate_seconds": round(seconds, 1),
        "estimate_usd": round(usd, 4),
        "transport": transport,
        "transport_refusal": transport_refusal,
    }


__all__ = [
    "MODAL_TIER_KEYS",
    "StemsPlanOut",
    "get_stems_plan",
    "router",
    "stems_transport_state",
]
