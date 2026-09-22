"""Per-key debouncer (SMART-03 support)."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Debouncer:
    """Arm/poll per-key debouncer.

    Pull-based so we can skip asyncio / TimerHandle threads; the trigger
    runner polls :meth:`ready` on its own cadence. ``clock`` is a hook
    for deterministic testing via a fake monotonic clock.
    """

    window_seconds: float = 5.0
    clock: Callable[[], float] = time.monotonic
    _armed_at: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._armed_at is None:
            self._armed_at = {}

    def arm(self, key: str, *, now: float | None = None) -> None:
        self._armed_at[key] = self.clock() if now is None else now

    def ready(self, *, now: float | None = None) -> list[str]:
        t = self.clock() if now is None else now
        expired = [k for k, ts in self._armed_at.items()
                   if t - ts >= self.window_seconds]
        for k in expired:
            del self._armed_at[k]
        return expired

    def cancel(self, key: str) -> None:
        self._armed_at.pop(key, None)

    def pending(self) -> list[str]:
        return list(self._armed_at.keys())


__all__ = ["Debouncer"]
