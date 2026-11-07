"""Pure provision-policy decisions for the Carabiner supervisor (LINK-122..124).

No I/O: callers pass probe results and host facts; this module says reuse, spawn, or refuse.
"""

from __future__ import annotations

from typing import Literal

#: Seconds before respawn attempt 1, 2, 3 ...; the last value repeats (capped, never a hot loop).
RESPAWN_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)

Decision = Literal["reuse", "spawn", "refuse_platform", "refuse_sandbox", "refuse_port_held"]


def decide_provision(*, port_open: bool, answers_status: bool, platform: str, sandboxed: bool) -> Decision:
    """What to do after the client's dial found nothing: reuse, spawn, or refuse (and why)."""
    if port_open and answers_status:
        return "reuse"
    if port_open:
        return "refuse_port_held"
    if platform != "darwin":
        return "refuse_platform"
    if sandboxed:
        return "refuse_sandbox"
    return "spawn"


def respawn_delay_s(attempt: int, backoff: tuple[float, ...] = RESPAWN_BACKOFF_S) -> float:
    """Seconds before respawn ``attempt`` (1-based), capped at the last step."""
    return backoff[min(max(attempt, 1), len(backoff)) - 1]


def unmanaged_note(platform: str) -> str:
    return (
        f"Open DJ starts Carabiner by itself only on macOS; on {platform} start Carabiner yourself "
        "(docs/b2b-link/FRIEND-SETUP.md)."
    )


__all__ = [
    "RESPAWN_BACKOFF_S",
    "Decision",
    "decide_provision",
    "respawn_delay_s",
    "unmanaged_note",
]
