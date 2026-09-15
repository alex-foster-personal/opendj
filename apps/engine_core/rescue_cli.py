"""``python -m apps.engine_core rescue list|restore`` against a running engine."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from apps.engine_core.rescue_api import RESTORE_PATH, SNAPSHOTS_PATH

EXIT_OK = 0
EXIT_TRANSPORT = 1
EXIT_REFUSED = 2


def _default_port() -> int:
    raw = os.environ.get("MUSIC_DJ_BACKEND_PORT")
    if raw is not None:
        return int(raw)
    return 8585


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _request(
    method: str,
    url: str,
    body: dict | None = None,
) -> tuple[int, dict | list | str]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise RuntimeError(f"rescue CLI transport error: {exc}") from exc
    if not raw:
        return status, {}
    return status, json.loads(raw)


def build_rescue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.engine_core rescue")
    sub = parser.add_subparsers(dest="rescue_command", required=True)

    list_cmd = sub.add_parser("list", help="list rescue snapshots")
    list_cmd.add_argument("--data-dir", required=True)
    list_cmd.add_argument("--port", type=int, default=_default_port())
    list_cmd.add_argument("--json", action="store_true")

    restore_cmd = sub.add_parser("restore", help="plan/validate a rescue restore")
    restore_cmd.add_argument("--data-dir", required=True)
    restore_cmd.add_argument("--port", type=int, default=_default_port())
    restore_cmd.add_argument("--play", action="store_true")
    restore_cmd.add_argument("--snapshot-id")
    restore_cmd.add_argument("--json", action="store_true")
    return parser


def run_rescue(argv: Sequence[str] | None = None) -> int:
    args = build_rescue_parser().parse_args(argv)
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"[ERROR] --data-dir {data_dir} does not exist", file=sys.stderr)
        return EXIT_TRANSPORT
    base = _base_url(args.port)
    try:
        if args.rescue_command == "list":
            status, payload = _request("GET", f"{base}{SNAPSHOTS_PATH}")
            if status != 200:
                print(json.dumps(payload, indent=2), file=sys.stderr)
                return EXIT_REFUSED if status in {404, 422} else EXIT_TRANSPORT
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                snapshots = payload.get("snapshots", [])
                for row in snapshots:
                    print(
                        f"{row['id']} age_ms={row['age_ms']} "
                        f"decks={row['deck_count_loaded']} "
                        f"playlist={row.get('playlist_id')}"
                    )
            return EXIT_OK

        body = {"play": bool(args.play)}
        if args.snapshot_id:
            body["snapshot_id"] = args.snapshot_id
        status, payload = _request("POST", f"{base}{RESTORE_PATH}", body)
        if status != 200:
            print(json.dumps(payload, indent=2), file=sys.stderr)
            return EXIT_REFUSED if status in {404, 422} else EXIT_TRANSPORT
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(json.dumps(payload, indent=2))
        return EXIT_OK
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_TRANSPORT


__all__ = ["build_rescue_parser", "run_rescue"]
