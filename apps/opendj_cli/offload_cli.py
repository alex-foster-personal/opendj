"""``opendj offload``: the CLI twin of the "Offload to cloud" menu (OFFLOAD).

    opendj offload tracks <stable_id>... [--wait]
    opendj offload playlist <playlist_id> [--wait]
    opendj offload restore <stable_id>... --for-24h|--permanent [--wait]
    opendj offload restore-playlist <playlist_id> --for-24h|--permanent [--wait]
    opendj offload status <stable_id>...
    opendj offload summary
    opendj offload op <op_id> | ops
    opendj offload cancel <op_id>
    opendj offload sweep [--dry-run] [--wait]

Each verb is one call to ``/api/v1/offload`` on the running engine, the same
routes the context menu and the ``offload_*`` MCP tools call
(spec ``(private path)`` section 8). ``--wait`` polls the
operation until it ends and exits non-zero unless it finished ``done``.

-Claude
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

OFFLOAD_PATH = "/api/v1/offload"
REQUEST_TIMEOUT_S = 60.0
POLL_S = 1.0
USAGE = "usage: opendj offload tracks|playlist|restore|restore-playlist|status|summary|op|ops|cancel|sweep ..."


def _mode(args: argparse.Namespace) -> str:
    if args.for_24h == args.permanent:
        raise ValueError("restore needs exactly one of --for-24h or --permanent")
    return "temporary" if args.for_24h else "permanent"


#: verb -> (method, path, body) for already-validated ids.
_ROUTES: dict[str, Callable[[list[str], argparse.Namespace], tuple[str, str, dict[str, Any] | None]]] = {
    "tracks": lambda ids, _a: ("POST", f"{OFFLOAD_PATH}/tracks", {"stable_ids": ids}),
    "playlist": lambda ids, _a: ("POST", f"{OFFLOAD_PATH}/playlists/{ids[0]}", None),
    "restore": lambda ids, a: ("POST", f"{OFFLOAD_PATH}/restore", {"stable_ids": ids, "mode": _mode(a)}),
    "restore-playlist": lambda ids, a: ("POST", f"{OFFLOAD_PATH}/playlists/{ids[0]}/restore", {"mode": _mode(a)}),
    "status": lambda ids, _a: ("GET", f"{OFFLOAD_PATH}/tracks?stable_ids={','.join(ids)}", None),
    "summary": lambda _i, _a: ("GET", f"{OFFLOAD_PATH}/summary", None),
    "op": lambda ids, _a: ("GET", f"{OFFLOAD_PATH}/operations/{ids[0]}", None),
    "ops": lambda _i, _a: ("GET", f"{OFFLOAD_PATH}/operations", None),
    "cancel": lambda ids, _a: ("POST", f"{OFFLOAD_PATH}/operations/{ids[0]}/cancel", None),
    "sweep": lambda _i, a: ("POST", f"{OFFLOAD_PATH}/sweep", {"dry_run": a.dry_run}),
}
_ONE_ID = frozenset({"playlist", "restore-playlist", "op", "cancel"})
_SOME_IDS = frozenset({"tracks", "restore", "status"})
_NO_IDS = frozenset({"summary", "ops", "sweep"})


def request_for(args: argparse.Namespace) -> tuple[str, str, dict[str, Any] | None]:
    """(method, path, body) for one parsed verb; ValueError on a bad combination."""
    ids: list[str] = args.ids
    verb: str = args.verb
    if verb in _ONE_ID and len(ids) != 1:
        raise ValueError(f"{verb} takes exactly one id")
    if verb in _SOME_IDS and not ids:
        raise ValueError(f"{verb} needs at least one stable_id")
    if verb in _NO_IDS and ids:
        raise ValueError(f"{verb} takes no ids")
    return _ROUTES[verb](ids, args)


def _progress_line(op: dict[str, Any]) -> str:
    gb = 1024**3
    return (
        f"{op['op_id']} {op['kind']} {op['state']}: {op['tracks_done']}/{op['tracks_total']} tracks, "
        f"{op['bytes_done'] / gb:.2f}/{op['bytes_total'] / gb:.2f} GB"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opendj offload", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=tuple(_ROUTES))
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--for-24h", action="store_true", help='"Download for 24h"')
    parser.add_argument("--permanent", action="store_true", help='"Re-download permanently"')
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait", action="store_true", help="poll the operation until it ends")
    return parser


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    try:
        args = _parser().parse_args(list(rest))
        method, path, body = request_for(args)
    except SystemExit:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    except ValueError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    try:
        base = resolve_backend_base_url(lock_path=lock).base_url.rstrip("/")
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            response = client.request(method, f"{base}{path}", json=body)
            if not response.is_success:
                return _fail(as_json, "request_failed",
                             f"{method} {path} returned {response.status_code}: {response.text}", EXIT_FAILED)
            payload = response.json()
            if args.wait and isinstance(payload, dict) and "op_id" in payload:
                while payload["state"] in ("queued", "running"):
                    if not as_json:
                        print(_progress_line(payload), file=sys.stderr)
                    time.sleep(POLL_S)
                    payload = client.get(f"{base}{OFFLOAD_PATH}/operations/{payload['op_id']}").json()
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if as_json or not (isinstance(payload, dict) and "op_id" in payload):
        print(json.dumps(payload, indent=2))
    else:
        print(_progress_line(payload))
        for sid, result in payload["results"].items():
            print(f"  {sid}: {result['state'] or result['error']} {result.get('note') or ''}".rstrip())
    failed = args.wait and isinstance(payload, dict) and payload.get("state") not in (None, "done")
    return EXIT_FAILED if failed else EXIT_CONFIRMED


__all__ = ["request_for", "run"]
