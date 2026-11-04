"""``opendj artwork``: store cover suggestions waiting on consent (LIBM-189).

    opendj artwork list
    opendj artwork accept --stable-id S [--stable-id T ...] | --all
    opendj artwork skip --stable-id S [--stable-id T ...] | --all

Same documents as ``GET /api/v1/artwork/suggestions`` and
``POST /api/v1/artwork/suggestions/decide`` and the MCP tools
``artwork_suggestions_list`` / ``artwork_suggestions_decide``. Deezer and
iTunes terms do not allow caching their content: accept only covers for
tracks you own and bought on their platform.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

SUGGESTIONS_PATH = "/api/v1/artwork/suggestions"
REQUEST_TIMEOUT_S = 120.0
USAGE = "usage: opendj artwork list | accept|skip (--stable-id S ... | --all)"


def request_for(verb: str, stable_ids: Sequence[str], every: bool) -> tuple[str, str, dict[str, Any] | None]:
    """(method, path, body) for one verb; raises ValueError on a bad combination."""
    if verb == "list":
        if stable_ids or every:
            raise ValueError("list takes no --stable-id or --all")
        return "GET", SUGGESTIONS_PATH, None
    if verb in ("accept", "skip"):
        if bool(stable_ids) == every:
            raise ValueError(f"{verb} needs --stable-id ... or --all, not both")
        body: dict[str, Any] = {f"{verb}_all": True} if every else {verb: list(stable_ids)}
        return "POST", f"{SUGGESTIONS_PATH}/decide", body
    raise ValueError(f"unhandled verb {verb!r}")


def _text(payload: dict[str, Any]) -> str:
    lines = [
        f"{sid}: {r['decision']} {r.get('source') or r.get('reason', '')}".rstrip()
        for sid, r in payload.get("results", {}).items()
    ]
    lines += [
        f"{s['stable_id']}  {s['source_label']}: {s['track_artist']} - {s['track_title']}" for s in payload["pending"]
    ]
    lines.append(f"{len(payload['pending'])} pending. {payload['note']}")
    return "\n".join(lines)


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opendj artwork", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=("list", "accept", "skip"))
    parser.add_argument("--stable-id", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    try:
        args = parser.parse_args(list(rest))
        method, path, body = request_for(args.verb, args.stable_id, args.all)
    except SystemExit:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
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
        return _fail(
            as_json, "request_failed", f"{method} {path} returned {response.status_code}: {response.text}", EXIT_FAILED
        )
    payload = response.json()
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else _text(payload))
    return EXIT_CONFIRMED
