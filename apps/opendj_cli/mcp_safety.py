"""Server-side safety rails for ``opendj mcp``.

Play orders require master mute first, writeback HTTP surfaces are refused,
and destructive library verbs stay gated unless ``OPENDJ_MCP_ENABLE_DESTRUCTIVE=1``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from apps.opendj_cli.orders import SEQUENCE, SINGLE
from apps.shared.rekordbox_writeback import WRITE_SURFACES

ENABLE_DESTRUCTIVE_ENV = "OPENDJ_MCP_ENABLE_DESTRUCTIVE"

# Curated destructive library paths (method-sensitive). Tested in unit tests.
_DESTRUCTIVE_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DELETE", re.compile(r"^/api/v1/.*")),
    ("POST", re.compile(r"^/api/v1/ingest/")),
    ("POST", re.compile(r"^/api/v1/playlists/[^/]+/tracks$")),
)

_WRITEBACK_HTTP_MARKERS: tuple[str, ...] = tuple(
    marker
    for surface in WRITE_SURFACES
    if surface.kind == "http" and surface.gated
    for marker in ("/writeback/apply", "/relocate/", "/usb-export/apply")
    if marker in surface.entrypoint.split(" ", 1)[-1]
)


class SafetyRefusal(ValueError):
    """A safety rail blocked the call before dispatch."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


def destructive_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENABLE_DESTRUCTIVE_ENV, "").strip() in {"1", "true", "yes", "on"}


def _is_audible_play(command: Mapping[str, Any]) -> bool:
    return command.get("type") == "play" and command.get("playing") is True


def _commands_in_order(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    if SINGLE in order:
        payload = order[SINGLE]
        if isinstance(payload, dict):
            return [payload]
        return []
    for kind in (SEQUENCE, "parallel"):
        payload = order.get(kind)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    if "ramp" in order:
        payload = order["ramp"]
        if isinstance(payload, dict):
            command = payload.get("command")
            if isinstance(command, dict):
                return [command]
    return []


def order_starts_audible_play(order: Mapping[str, Any]) -> bool:
    return any(_is_audible_play(command) for command in _commands_in_order(order))


def master_is_muted(mirror_delta: Mapping[str, Any] | None) -> bool:
    if not isinstance(mirror_delta, dict):
        return False
    changed = mirror_delta.get("changed")
    if not isinstance(changed, dict):
        return False
    master = changed.get("master")
    if isinstance(master, dict) and master.get("muted") is True:
        return True
    mirror = changed.get("mirror")
    if mirror is True:
        # The test page publishes a coarse delta; callers may have muted earlier.
        return False
    return False


def prepend_master_mute(order: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return an order with master mute first when a play would be audible."""
    mute_command = {"type": "master_mute", "muted": True}
    if SINGLE in order:
        return {SEQUENCE: [mute_command, order[SINGLE]]}, True
    commands = _commands_in_order(order)
    if not commands:
        raise SafetyRefusal(
            "play_blocked",
            "could not prepend master mute to an order with no commands",
        )
    return {SEQUENCE: [mute_command, *commands]}, True


def guard_order(
    order: dict[str, Any],
    *,
    last_mirror_delta: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Apply play gate; return possibly rewritten order and safety metadata."""
    if not order_starts_audible_play(order):
        return order, None
    if master_is_muted(last_mirror_delta):
        return order, None
    rewritten, prepended = prepend_master_mute(order)
    return rewritten, {"prepended_master_mute": prepended}


def validate_api_path(path: str) -> None:
    if not path.startswith("/api/v1/"):
        raise SafetyRefusal(
            "invalid_path",
            f"app_state only proxies /api/v1/* paths, got: {path}",
            path=path,
        )
    if ".." in path:
        raise SafetyRefusal(
            "invalid_path",
            f"path traversal is not allowed: {path}",
            path=path,
        )


def guard_library_request(method: str, path: str, environ: Mapping[str, str] | None = None) -> None:
    upper = method.upper()
    for marker in _WRITEBACK_HTTP_MARKERS:
        if marker in path:
            raise SafetyRefusal(
                "writeback_blocked",
                f"writeback surface blocked: {upper} {path}",
                path=path,
            )
    if destructive_enabled(environ):
        return
    for blocked_method, pattern in _DESTRUCTIVE_PATH_PATTERNS:
        if upper == blocked_method and pattern.match(path):
            raise SafetyRefusal(
                "destructive_blocked",
                f"destructive library call blocked: {upper} {path}",
                path=path,
                method=upper,
            )
