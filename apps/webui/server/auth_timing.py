"""Wall-clock timing for auth route handlers (S13 server phases).

Split out of :mod:`apps.webui.server.routes.auth` so the route module stays a
thin call site. Tests read :func:`last_capture`; ``POST /login`` also exposes
``X-OpenDJ-Auth-Ms``.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

_inject_delay_s: float = 0.0
_last: dict[str, float] | None = None


def inject_delay(seconds: float) -> None:
    """Test-only hook: sleep inside the next timed span(s). Default no-op."""
    global _inject_delay_s
    _inject_delay_s = seconds


def reset_inject_delay() -> None:
    global _inject_delay_s
    _inject_delay_s = 0.0


def last_capture() -> dict[str, float] | None:
    """The most recent ``{op, duration_ms}`` from :func:`AuthTimer.span``."""
    return _last


class AuthTimer:
    @classmethod
    @contextmanager
    def span(cls, op: str) -> Iterator[None]:
        global _last
        t0 = time.perf_counter()
        try:
            if _inject_delay_s:
                time.sleep(_inject_delay_s)
            yield
        finally:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            _last = {"op": op, "duration_ms": duration_ms}


__all__ = ["AuthTimer", "inject_delay", "last_capture", "reset_inject_delay"]
