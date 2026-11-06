"""CLI for the Rekordbox ANALYSE-ME flow (agent-native parity with the HTTP and MCP surfaces).

    python -m apps.rekordbox_analyse plan
    python -m apps.rekordbox_analyse apply --plan-sha <sha> --cloud-sync-off
    python -m apps.rekordbox_analyse adopt

Every command prints one JSON document. A refused or failed apply exits 2
with its ``code``; nothing is written on a refusal.

-Claude
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.rekordbox_analyse import service
from apps.rekordbox_analyse.writer import AnalyseApplyFailed, AnalyseApplyRefused
from apps.shared.rekordbox_writeback import RekordboxWritebackDisabled


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.rekordbox_analyse")
    parser.add_argument("--state-db", type=Path, help="Open DJ state.db (default: the repo data dir)")
    parser.add_argument("--backups-root", type=Path, help="where Rekordbox backups go")
    parser.add_argument(
        "--only-playlists",
        action="append",
        default=[],
        metavar="PREFIX",
        help="limit to tracks in Open DJ playlists whose name starts with PREFIX (repeatable)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="show what one write would do (read-only)")
    apply = sub.add_parser("apply", help="write the confirmed plan into Rekordbox (guarded)")
    apply.add_argument("--plan-sha", required=True, help="plan_sha from the plan you confirmed")
    apply.add_argument("--cloud-sync-off", action="store_true", help="confirm Rekordbox Cloud Sync is off")
    adopt = sub.add_parser("adopt", help="bring in-scope Rekordbox analysis into Open DJ (read-only on Rekordbox)")
    adopt.add_argument("--dry-run", action="store_true", help="report only; Open DJ is not written")
    run = sub.add_parser(
        "run",
        help="the whole round in order: adopt analysed tracks, re-plan, write new ones if Rekordbox is quit, "
        "list what MIK still needs, say what is left for the maintainer",
    )
    run.add_argument("--cloud-sync-off", action="store_true", help="confirm Rekordbox Cloud Sync is off")
    return parser


def _where(args: argparse.Namespace) -> service.Locations:
    base = service.locations()
    return service.Locations(base.live_db, args.state_db or base.state_db, args.backups_root or base.backups_root)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    prefixes = tuple(args.only_playlists)
    if args.command == "plan":
        print(json.dumps(service.plan_status(_where(args), prefixes), indent=2, default=str))
        return 0
    if args.command == "apply":
        try:
            report = service.apply(
                args.plan_sha, cloud_sync_off_confirmed=args.cloud_sync_off, where=_where(args), prefixes=prefixes
            )
        except (AnalyseApplyRefused, AnalyseApplyFailed, RekordboxWritebackDisabled) as error:
            print(json.dumps(error.to_dict(), indent=2, default=str))
            return 2
        print(json.dumps(report.to_dict(), indent=2, default=str))
        return 0
    if args.command == "adopt":
        print(json.dumps(service.adopt(_where(args), prefixes, dry_run=args.dry_run), indent=2))
        return 0
    if args.command == "run":
        report = service.run_round(_where(args), prefixes, cloud_sync_off_confirmed=args.cloud_sync_off)
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["ok"] else 2
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
