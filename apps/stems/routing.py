"""Which stems executor runs: Modal farm or on-device local worker.

Selected ONLY from the entitlement-selected CloudSync policy plus farm
availability. Callers do not pass a mode argument (CLOUDSYNC-01).

Farm wins when remote processing is allowed AND the build can reach a GPU.
Local is the fallback for local-only policy, not a replacement for farm.
"""

from __future__ import annotations

EXECUTOR_MODAL: str = "modal"
EXECUTOR_LOCAL: str = "local"
EXECUTORS: frozenset[str] = frozenset({EXECUTOR_MODAL, EXECUTOR_LOCAL})


def resolve_stems_executor(
    *,
    remote_processing_allowed: bool | None = None,
) -> str:
    """Return EXECUTOR_MODAL or EXECUTOR_LOCAL for this build right now."""
    from apps.cloud.policy import CFG

    remote = (
        CFG.remote_processing_allowed
        if remote_processing_allowed is None
        else remote_processing_allowed
    )
    if not remote:
        return EXECUTOR_LOCAL
    from apps.stems.api import stems_transport_state

    _transport, refusal = stems_transport_state()
    if refusal is None:
        return EXECUTOR_MODAL
    return EXECUTOR_LOCAL


def effective_tier(requested: str, executor: str) -> str:
    """Modal keeps S/M/L; local always runs the LOCAL rung."""
    if executor == EXECUTOR_LOCAL:
        return "LOCAL"
    return requested


__all__ = [
    "EXECUTORS",
    "EXECUTOR_LOCAL",
    "EXECUTOR_MODAL",
    "effective_tier",
    "resolve_stems_executor",
]
