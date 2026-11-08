"""``opendj edit`` parity for non-destructive virtual track edits."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from apps.opendj_cli import EXIT_FAILED, api_cli

DEFAULT_UNDO_SESSION = "opendj-cli"
_API = "/api/v1/edits"


@dataclass(frozen=True)
class EditRequest:
    method: str
    path: str
    body: Mapping[str, Any] | None = None

    @property
    def is_write(self) -> bool:
        return self.method != "GET"


def _fx(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("--fx must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("--fx must be a JSON object")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opendj edit", exit_on_error=False)
    parser.add_argument("--undo-session", default=DEFAULT_UNDO_SESSION)
    verbs = parser.add_subparsers(dest="verb", required=True)

    create = verbs.add_parser("create", exit_on_error=False)
    create.add_argument("--source", required=True)
    create.add_argument("--label", default="")
    create.add_argument("--in-ms", type=int, required=True)
    create.add_argument("--out-ms", type=int, required=True)
    create.add_argument("--fx", type=_fx, default={})
    create.add_argument("--playlist")
    create.add_argument("--after-item")

    update = verbs.add_parser("update", exit_on_error=False)
    update.add_argument("edit")
    update.add_argument("--label")
    update.add_argument("--in-ms", type=int)
    update.add_argument("--out-ms", type=int)
    update.add_argument("--fx", type=_fx)

    remove = verbs.add_parser("rm", exit_on_error=False)
    remove.add_argument("edit")
    return parser


def build_request(rest: Sequence[str]) -> tuple[EditRequest, str]:
    args = _parser().parse_args(list(rest))
    if args.verb == "create":
        body: dict[str, Any] = {
            "source_track_id": args.source,
            "label": args.label,
            "in_ms": args.in_ms,
            "out_ms": args.out_ms,
            "fx": args.fx,
        }
        if args.playlist is not None:
            body["playlist_id"] = args.playlist
        if args.after_item is not None:
            body["after_item_id"] = args.after_item
        request = EditRequest("POST", _API, body)
    elif args.verb == "update":
        body = {
            name: getattr(args, name) for name in ("label", "in_ms", "out_ms", "fx") if getattr(args, name) is not None
        }
        request = EditRequest("PATCH", f"{_API}/{quote(args.edit, safe='')}", body)
    else:
        request = EditRequest("DELETE", f"{_API}/{quote(args.edit, safe='')}")
    return request, args.undo_session


def run(
    rest: Sequence[str],
    *,
    as_json: bool = False,
    environ: Mapping[str, str] | None = None,
    lock: Path | None = None,
) -> int:
    try:
        request, undo_session = build_request(rest)
    except (argparse.ArgumentError, SystemExit) as error:
        print(f"[ERROR] opendj edit: {error}", file=sys.stderr)
        return EXIT_FAILED
    tokens = [request.method, request.path, "-H", f"X-Undo-Session:{undo_session}"]
    if request.body is not None:
        tokens += ["--json", json.dumps(request.body)]
    return api_cli.run(tokens, as_json=as_json, environ=environ, lock=lock)


__all__ = ["EditRequest", "build_request", "run"]
