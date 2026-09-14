"""``verdicts`` subcommand of ``python -m apps.lyrics``: the library-scale
stem-coverage ``lyric_verdict`` backfill (LYR-06).

Own module so ``apps/lyrics/__main__.py`` stays under the 600-line gate, same
split as :mod:`apps.lyrics.cli_pipeline` / :mod:`apps.lyrics.cli_storage`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.lyrics import library_verdicts
from apps.shared.paths import DATA_DIR
from apps.shared.state import db as state_db

VERDICTS_COMMANDS: frozenset[str] = frozenset({"verdicts"})


def add_verdicts_commands(subcommands: argparse._SubParsersAction) -> None:
    verdicts = subcommands.add_parser(
        "verdicts", help="library-scale lyric_verdict backfill from stem bundles"
    )
    verdicts_sub = verdicts.add_subparsers(dest="verdicts_command", required=True)

    backfill = verdicts_sub.add_parser(
        "backfill",
        help="fill/refresh coverage-only verdicts for every loadable stem bundle",
    )
    backfill.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help=f"data root holding state/state.db and state/stems (default: {DATA_DIR})",
    )
    backfill.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max tracks to COMPUTE this run (default: no cap)",
    )
    backfill.add_argument(
        "--include-reserved",
        action="store_true",
        help=(
            "also process the 100 stable_ids held back in "
            f"{library_verdicts.RESERVED_FILENAME} for in-app ordering QA "
            "(refused by default)"
        ),
    )
    backfill.add_argument(
        "--json", action="store_true", help="machine-readable summary on stdout"
    )
    mode = backfill.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="plan only, no writes")
    mode.add_argument(
        "--live", action="store_true", help="write lyric_verdict rows"
    )


def cmd_verdicts(args: argparse.Namespace) -> int:
    if args.command != "verdicts":
        raise AssertionError(f"unhandled command {args.command!r}")
    if args.verdicts_command != "backfill":
        raise AssertionError(f"unhandled verdicts subcommand {args.verdicts_command!r}")
    if args.limit is not None and args.limit < 0:
        print("[ERROR] --limit must be >= 0", file=sys.stderr)
        return 1
    conn = state_db.open_rw(args.data_dir / "state" / "state.db")
    try:
        report = library_verdicts.backfill_verdicts(
            conn,
            data_dir=args.data_dir,
            dry_run=args.dry_run,
            limit=args.limit,
            include_reserved=args.include_reserved,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(library_verdicts.report_to_dict(report), indent=1))
    else:
        print(library_verdicts.format_report(report))
    return 1 if report.failed else 0


__all__ = ["VERDICTS_COMMANDS", "add_verdicts_commands", "cmd_verdicts"]
