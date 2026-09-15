"""In-memory pending shell navigation store (issue #2866).

Pure logic for unit tests; HTTP routes in :mod:`routes.shell`.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

_ALLOWED_PREFIX = "/performance"


@dataclass(frozen=True)
class PendingNavigate:
    id: str
    route: str


class ShellNavigateStore:
    """Single pending slot per engine process; last POST wins."""

    def __init__(self) -> None:
        self._pending: PendingNavigate | None = None

    def set_pending(self, route: str) -> PendingNavigate:
        pending = PendingNavigate(id=uuid4().hex, route=route)
        self._pending = pending
        return pending

    def get_pending(self) -> PendingNavigate | None:
        return self._pending

    def ack(self, navigate_id: str) -> bool:
        if self._pending is None or self._pending.id != navigate_id:
            return False
        self._pending = None
        return True


def validate_shell_route(route: str) -> str:
    """Reject external URLs and routes outside the performance allowlist."""
    if not route or not route.startswith("/"):
        raise ValueError("route must start with /")
    if route.startswith("//"):
        raise ValueError("route must not start with //")
    if "://" in route:
        raise ValueError("route must be a path, not a URL")
    if route != _ALLOWED_PREFIX and not route.startswith(f"{_ALLOWED_PREFIX}/"):
        raise ValueError(f"route must be {_ALLOWED_PREFIX} or a subpath")
    return route
