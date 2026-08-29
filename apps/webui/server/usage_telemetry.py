"""In-memory app-usage telemetry: is a client open, and is anyone looking?

The engine must be able to answer "is the desktop app running right now"
without a human being asked. Two independent signals feed one answer:

- ACTIVE: clients POST a heartbeat carrying their surface and page
  visibility. This is the authoritative signal.
- PASSIVE: ordinary HTTP traffic classified by User-Agent, as a backstop for
  a client too old (or too broken) to heartbeat.

Deliberately in-memory and unpersisted. The question is about NOW, so an
engine restart resetting the answer to "nothing has checked in yet" is the
truth, not a loss. Nothing here is ever inferred when it can be measured:
a client that has not heartbeat is reported stale, never assumed present.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, get_args

Surface = Literal["desktop-shell", "browser"]
SURFACES: tuple[Surface, ...] = get_args(Surface)

#: A client counts as present only if it checked in this recently. Three
#: missed 15s heartbeats, so one dropped request never reads as "closed".
IN_USE_WINDOW_SECONDS: float = 45.0

#: Paths whose traffic says nothing about a human using the app: the
#: telemetry endpoints themselves (an agent asking the question would
#: otherwise answer it) and health polling (agents, probes, the desktop
#: shell's own bootstrap loop all hit it while nobody is looking).
PASSIVE_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/api/v1/telemetry",
    "/api/v1/health",
)


def classify_user_agent(user_agent: str) -> Surface | None:
    """Which app surface a request came from, or None for a non-app client.

    Evidence, captured Wed 19 Aug 2026 by attaching the real installed
    Open DJ.app 0.1.0 to a loopback probe engine: the shell's WKWebView
    sends ``Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)
    AppleWebKit/605.1.15 (KHTML, like Gecko)`` -- AppleWebKit with no
    ``Safari/`` token, which is what separates a bare WKWebView from Safari
    and Chrome (both carry ``Safari/``). curl, httpx and the agents driving
    this API carry neither, so they classify as None and can never move the
    usage needle.
    """
    if "AppleWebKit/" in user_agent and "Safari/" not in user_agent:
        return "desktop-shell"
    if (
        "Safari/" in user_agent
        or "Chrome/" in user_agent
        or "Firefox/" in user_agent
    ):
        return "browser"
    return None


def counts_as_app_usage(path: str, user_agent: str) -> Surface | None:
    """The surface a request is evidence of, or None if it proves nothing."""
    if path.startswith(PASSIVE_EXCLUDED_PREFIXES):
        return None
    return classify_user_agent(user_agent)


@dataclass(frozen=True)
class Seen:
    """One server-stamped sighting: monotonic for math, wall clock to read."""

    monotonic: float
    wall_clock: str


@dataclass(frozen=True)
class ClientHeartbeat:
    client_id: str
    surface: Surface
    page_visible: bool
    app_version: str
    seen: Seen


def _utc_now() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class UsageStore:
    """Every client seen since engine boot, keyed by client id.

    ``monotonic`` is the one injected seam, so tests assert the in-use
    boundary without sleeping through it. Elapsed time is measured on the
    monotonic clock (immune to wall-clock jumps); the wall clock is recorded
    alongside purely so a human or agent can read when it happened.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._clients: dict[str, ClientHeartbeat] = {}
        self._passive: dict[Surface, Seen] = {}

    # ----------------------------------------------------------- writes

    def record_heartbeat(
        self,
        *,
        client_id: str,
        surface: Surface,
        page_visible: bool,
        app_version: str,
    ) -> ClientHeartbeat:
        """Upsert one client. The timestamp is the server's, never the client's."""
        entry = ClientHeartbeat(
            client_id=client_id,
            surface=surface,
            page_visible=page_visible,
            app_version=app_version,
            seen=self._now(),
        )
        self._clients[client_id] = entry
        return entry

    def record_request(self, *, path: str, user_agent: str) -> Surface | None:
        """Fold one ordinary HTTP request into the passive signal."""
        surface = counts_as_app_usage(path, user_agent)
        if surface is not None:
            self._passive[surface] = self._now()
        return surface

    # ----------------------------------------------------------- reads

    def snapshot(self) -> dict:
        """The whole answer in one shape: rows, summary, passive backstop."""
        now = self._monotonic()
        clients = [
            self._client_view(entry, now)
            for entry in sorted(
                self._clients.values(), key=lambda item: item.seen.monotonic
            )
        ]
        return {
            "clients": clients,
            "summary": {
                "any_client_open": any(row["is_open"] for row in clients),
                "any_client_in_use": any(row["in_use"] for row in clients),
                "desktop_shell_open": any(
                    row["is_open"] and row["surface"] == "desktop-shell"
                    for row in clients
                ),
            },
            "passive_activity": self._passive_view(now),
            "in_use_window_seconds": IN_USE_WINDOW_SECONDS,
        }

    # ----------------------------------------------------------- _helpers

    def _now(self) -> Seen:
        return Seen(monotonic=self._monotonic(), wall_clock=self._wall_clock())

    @staticmethod
    def _client_view(entry: ClientHeartbeat, now: float) -> dict:
        elapsed = round(now - entry.seen.monotonic, 3)
        is_open = elapsed <= IN_USE_WINDOW_SECONDS
        return {
            "client_id": entry.client_id,
            "surface": entry.surface,
            "page_visible": entry.page_visible,
            "app_version": entry.app_version,
            "last_seen_at": entry.seen.wall_clock,
            "seconds_since_seen": elapsed,
            # Open = checked in recently. In use = open AND the page is on
            # screen; a minimised window is running but nobody is looking.
            "is_open": is_open,
            "in_use": is_open and entry.page_visible,
        }

    def _passive_view(self, now: float) -> dict:
        by_surface = {
            surface: {
                "last_request_at": (
                    self._passive[surface].wall_clock
                    if surface in self._passive
                    else None
                ),
                "seconds_since_request": (
                    round(now - self._passive[surface].monotonic, 3)
                    if surface in self._passive
                    else None
                ),
            }
            for surface in SURFACES
        }
        latest = max(
            self._passive.values(), key=lambda seen: seen.monotonic, default=None
        )
        return {
            "last_request_at": latest.wall_clock if latest else None,
            "seconds_since_last_request": (
                round(now - latest.monotonic, 3) if latest else None
            ),
            "by_surface": by_surface,
        }


__all__ = [
    "IN_USE_WINDOW_SECONDS",
    "SURFACES",
    "ClientHeartbeat",
    "Surface",
    "UsageStore",
    "classify_user_agent",
    "counts_as_app_usage",
]
