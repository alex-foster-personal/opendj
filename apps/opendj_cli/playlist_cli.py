"""``opendj playlist thumbnail``: a playlist's art thumbnail (ARTTHUMB-8728b).

    opendj playlist thumbnail <playlist-id> [--size 64|232] [--out FILE]

Same image as ``GET /api/v1/playlists/{id}/thumbnail``, the one the sidebar
rows and the playlists card grid show. Prints the kind (cover or
placeholder), the content type and the byte count; ``--out`` also writes the
image to FILE.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

REQUEST_TIMEOUT_S = 60.0
USAGE = "usage: opendj playlist thumbnail <playlist-id> [--size 64|232] [--out FILE]"


def thumbnail_path(playlist_id: str, size: int) -> str:
    return f"/api/v1/playlists/{quote(playlist_id, safe='')}/thumbnail?size={size}"


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opendj playlist", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=("thumbnail",))
    parser.add_argument("playlist_id")
    parser.add_argument("--size", type=int, choices=(64, 232), default=64)
    parser.add_argument("--out", type=Path)
    try:
        args = parser.parse_args(list(rest))
    except SystemExit:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    path = thumbnail_path(args.playlist_id, args.size)
    try:
        target = resolve_backend_base_url(lock_path=lock)
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            response = client.get(f"{target.base_url.rstrip('/')}{path}")
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if not response.is_success:
        return _fail(as_json, "request_failed", f"GET {path} returned {response.status_code}: {response.text}",
                     EXIT_FAILED)
    if args.out is not None:
        args.out.write_bytes(response.content)
    payload = {
        "playlist_id": args.playlist_id,
        "size": args.size,
        "kind": response.headers["x-thumbnail-kind"],
        "content_type": response.headers["content-type"],
        "bytes": len(response.content),
        "out": str(args.out) if args.out is not None else None,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{payload['kind']} {payload['content_type']} {payload['bytes']} bytes"
              + (f" -> {payload['out']}" if payload["out"] else ""))
    return EXIT_CONFIRMED
