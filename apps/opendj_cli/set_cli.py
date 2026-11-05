"""``opendj set``: a playlist's set target length, skip-in-set flags and summary.

    opendj set summary <playlist_id>
    opendj set target <playlist_id> <minutes>|clear
    opendj set skip <playlist_id> <stable_id> [off]

Same documents as ``/api/v1/playlists/{id}/set-summary``, ``.../set-target``
and ``.../set-skips/{stable_id}``, and the MCP tools ``set_summary``,
``set_target`` and ``set_skip``.

-Claude
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

PLAYLISTS_PATH = "/api/v1/playlists"
REQUEST_TIMEOUT_S = 30.0
USAGE = ("usage: opendj set summary <playlist_id> | target <playlist_id> <minutes>|clear"
         " | skip <playlist_id> <stable_id> [off]")


def request_for(args: Sequence[str]) -> tuple[str, str, dict[str, Any] | None]:
    """(method, path, body) for one ``opendj set`` call; raises ValueError on bad usage."""
    if len(args) == 2 and args[0] == "summary":
        return "GET", f"{PLAYLISTS_PATH}/{args[1]}/set-summary", None
    if len(args) == 3 and args[0] == "target":
        target_s = None if args[2] == "clear" else float(args[2]) * 60.0
        if target_s is not None and target_s <= 0:
            raise ValueError("target minutes must be positive")
        return "PUT", f"{PLAYLISTS_PATH}/{args[1]}/set-target", {"target_s": target_s}
    if len(args) in (3, 4) and args[0] == "skip":
        if len(args) == 4 and args[3] != "off":
            raise ValueError(f"expected 'off', got {args[3]!r}")
        return "PUT", f"{PLAYLISTS_PATH}/{args[1]}/set-skips/{args[2]}", {"skipped": len(args) == 3}
    raise ValueError(USAGE)


def _text(payload: dict[str, Any]) -> str:
    lines = [f"{payload['playlist_id']}: {payload['readout']} ({payload['skipped']} skipped)"]
    lines += [f"key clash {c['from_key']} -> {c['to_key']} ({c['steps']} steps){' GROSS' if c['gross'] else ''}"
              f"  {c['from_id']} -> {c['to_id']}" for c in payload["clashes"]]
    return "\n".join(lines)


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    try:
        method, path, body = request_for(list(rest))
    except ValueError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    try:
        target = resolve_backend_base_url(lock_path=lock)
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            response = client.request(method, f"{target.base_url.rstrip('/')}{path}", json=body)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if not response.is_success:
        return _fail(as_json, "request_failed",
                     f"{method} {path} returned {response.status_code}: {response.text}", EXIT_FAILED)
    payload = response.json()
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else _text(payload))
    return EXIT_CONFIRMED
