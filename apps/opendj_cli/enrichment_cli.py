"""``opendj enrichment``: HTTP parity for the ingest enrichment coordinator (ENRICH-09).

    opendj enrichment status [--batch B]          every recent import batch, or one with its tracks
    opendj enrichment rerun --batch B | --playlist P

Same documents as ``GET /api/v1/ingest/enrichment[/{batch}]`` and
``POST /api/v1/ingest/enrichment/rerun`` and the MCP tools
``ingest_enrichment_status`` / ``ingest_enrichment_rerun``.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

ENRICHMENT_PATH = "/api/v1/ingest/enrichment"
REQUEST_TIMEOUT_S = 30.0


def request_for(verb: str, batch: str | None, playlist: str | None) -> tuple[str, str, dict[str, str] | None]:
    """(method, path, body) for one verb; raises ValueError on a bad combination."""
    if verb == "status":
        if playlist is not None:
            raise ValueError("status takes --batch only")
        return "GET", ENRICHMENT_PATH if batch is None else f"{ENRICHMENT_PATH}/{quote(batch, safe='')}", None
    if verb == "rerun":
        if (batch is None) == (playlist is None):
            raise ValueError("rerun needs exactly one of --batch or --playlist")
        body = {"batch_id": batch} if batch is not None else {"playlist_id": str(playlist)}
        return "POST", f"{ENRICHMENT_PATH}/rerun", body
    raise ValueError(f"unhandled verb {verb!r}")


def _text(payload: dict[str, Any]) -> str:
    lines = [f"[WARN] {w['lane']}: {w['message']} Fix: {w['fix']}" for w in payload.get("warnings", [])]
    for batch in payload.get("batches", []):
        lines.append(
            f"{batch['batch_id']} ({batch['source']}, {batch['tracks']} tracks, artwork {batch['artwork_stage']})"
        )
        for lane in payload.get("lane_priority", []):
            counts = ", ".join(f"{state} {n}" for state, n in sorted(batch["lanes"][lane].items()))
            lines.append(f"  {lane}: {counts}")
    if "ok" in payload:
        lines.append(f"rerun queued: {payload['batch_id']} ({payload['tracks']} tracks)")
    return "\n".join(lines)


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opendj enrichment", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=("status", "rerun"))
    parser.add_argument("--batch")
    parser.add_argument("--playlist")
    try:
        args = parser.parse_args(list(rest))
        method, path, body = request_for(args.verb, args.batch, args.playlist)
    except SystemExit:
        return _fail(
            as_json,
            "usage",
            "usage: opendj enrichment status [--batch B] | rerun --batch B | --playlist P",
            EXIT_FAILED,
        )
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
