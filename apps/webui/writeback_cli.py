"""CLI parity for the playlist writeback HTTP contract.

All commands call the same local HTTP endpoints as the UI.  ``apply`` and
``rollback`` require both a reviewed plan/backup token and ``--confirm``.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _request(base_url: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(f"{base_url.rstrip('/')}{path}", data=payload, method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urlopen(request) as response:  # noqa: S310 - explicit local daemon URL from caller
            return json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vendor", required=True, choices=("rekordbox", "djay"))
    parser.add_argument("--target-mode", required=True, choices=("live",))
    parser.add_argument("--target-path", required=True)
    parser.add_argument("--target-id", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.webui.writeback_cli")
    parser.add_argument("--base-url", default="http://127.0.0.1:8585/api/v1")
    commands = parser.add_subparsers(dest="command", required=True)
    capabilities = commands.add_parser("capabilities")
    capabilities.add_argument("playlist_id")
    targets = commands.add_parser("targets")
    targets.add_argument("playlist_id")
    targets.add_argument("--vendor", required=True, choices=("rekordbox", "djay"))
    targets.add_argument("--target-mode", required=True, choices=("live",))
    targets.add_argument("--target-path", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("playlist_id")
    _target_args(plan)
    apply = commands.add_parser("apply")
    apply.add_argument("playlist_id")
    _target_args(apply)
    apply.add_argument("--plan-token", required=True)
    apply.add_argument("--confirm", action="store_true")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("playlist_id")
    _target_args(rollback)
    rollback.add_argument("--backup-id", required=True)
    rollback.add_argument("--expected-target-revision", required=True)
    rollback.add_argument("--confirm", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = f"/playlists/{args.playlist_id}/writeback"
    try:
        if args.command == "capabilities":
            result = _request(args.base_url, "GET", f"{root}/capabilities")
        else:
            target_query = urlencode({"vendor": args.vendor, "target_mode": args.target_mode, "target_path": args.target_path})
        if args.command == "capabilities":
            pass
        elif args.command == "targets":
            result = _request(args.base_url, "GET", f"{root}/targets?{target_query}")
        elif args.command == "plan":
            result = _request(args.base_url, "GET", f"{root}/plan?{target_query}&{urlencode({'target_id': args.target_id})}")
        elif args.command == "apply":
            if not args.confirm:
                raise RuntimeError("apply refuses without --confirm")
            result = _request(args.base_url, "POST", f"{root}/apply", {
                "vendor": args.vendor, "target_mode": args.target_mode, "target_path": args.target_path,
                "target_id": args.target_id, "plan_token": args.plan_token, "dry_run": False, "confirmed": True,
            })
        else:
            if not args.confirm:
                raise RuntimeError("rollback refuses without --confirm")
            result = _request(args.base_url, "POST", f"{root}/rollback", {
                "vendor": args.vendor, "target_mode": args.target_mode, "target_path": args.target_path,
                "target_id": args.target_id, "backup_id": args.backup_id,
                "expected_target_revision": args.expected_target_revision, "confirmed": True,
            })
    except RuntimeError as exc:
        sys.stderr.write(f"writeback: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
