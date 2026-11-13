"""``opendj sync ratings``: the SYNC-06 ratings dry run and apply, at CLI parity with ``python -m apps.sync``.

    opendj sync ratings [--diff-csv PATH]                       # dry run (default)
    opendj sync ratings --live --tracks ID[,ID] --i-understand-the-risks
    opendj sync ratings --live --bulk --i-understand-the-risks

The plan and the writes stay in :mod:`apps.sync.apply_ratings`; this module only
parses the same flags and forwards them. A live run without
``--i-understand-the-risks`` is refused here, before any database is opened,
so an agent cannot reach the write path without the typed acknowledgement
the conflict policy in ``(private path)`` requires.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.refusal import fail
from apps.shared import paths

SYNC_USAGE = (
    "usage: opendj [--json] sync ratings [--diff-csv PATH] [--live] [--tracks IDS] [--bulk]\n"
    "                                    [--prefer rb|djay|newest] [--skip-cautious-check]\n"
    "                                    [--i-understand-the-risks]\n"
    "\n"
    "subcommands:\n"
    "  ratings  dry-run or apply a ratings diff CSV between Rekordbox and djay (SYNC-06)\n"
    "\n"
    "A run is a dry run unless --live is given; --live requires --i-understand-the-risks."
)
ACK_FLAG = "--i-understand-the-risks"


def default_diff_csv() -> Path:
    """The ratings diff CSV the module CLI reads when --diff-csv is not given."""
    return paths.DATA_DIR / "sync" / "ratings-diff.csv"


class _RatingsParser(argparse.ArgumentParser):
    """An argparse parser that raises instead of exiting, so usage errors keep the opendj envelope."""

    def error(self, message: str) -> None:  # type: ignore[override]
        """Raise the usage message for :func:`run` to report."""
        raise ValueError(message)


def ratings_parser() -> argparse.ArgumentParser:
    """The ``opendj sync ratings`` flags, mirroring ``python -m apps.sync apply-ratings``."""
    parser = _RatingsParser(
        prog="opendj sync ratings",
        description="Dry-run (default) or apply a ratings diff between Rekordbox and djay (SYNC-06).",
    )
    parser.add_argument(
        "--diff-csv", type=Path, default=None, help="ratings diff CSV (default (private path))"
    )
    parser.add_argument("--live", action="store_true", help="write the live databases instead of a dry run")
    parser.add_argument("--bulk", action="store_true", help="apply every row (needs a prior cautious --tracks run)")
    parser.add_argument("--tracks", default="", help="comma-separated rb content ids or djay uuids (cautious run)")
    parser.add_argument("--prefer", choices=["rb", "djay", "newest"], default="newest")
    parser.add_argument(
        "--skip-cautious-check", action="store_true", help="skip the dry-run, cautious, bulk order check"
    )
    parser.add_argument(
        ACK_FLAG,
        dest="i_understand_the_risks",
        action="store_true",
        help="typed acknowledgement required by every --live run",
    )
    return parser


def module_argv(args: argparse.Namespace) -> list[str]:
    """The ``apps.sync.apply_ratings`` argv for parsed ``opendj sync ratings`` args."""
    argv = ["--diff-csv", str(args.diff_csv or default_diff_csv()), "--prefer", args.prefer]
    if args.live:
        argv.append("--live")
    if args.bulk:
        argv.append("--bulk")
    if args.tracks:
        argv += ["--tracks", args.tracks]
    if args.skip_cautious_check:
        argv.append("--skip-cautious-check")
    if args.i_understand_the_risks:
        argv.append(ACK_FLAG)
    return argv


def _dry_run_json(diff_csv: Path) -> int:
    """Print the dry-run plan as JSON: row count and resolution counts, nothing written."""
    from apps.sync.apply_ratings import _summarise_plan
    from apps.sync.diff_csv import load_diff_rows

    rows = load_diff_rows(diff_csv)
    document = {
        "diff_csv": str(diff_csv),
        "dry_run": True,
        "resolutions": _summarise_plan(rows),
        "rows": len(rows),
    }
    print(json.dumps(document, indent=2, sort_keys=True))
    return EXIT_CONFIRMED


def run_ratings(tokens: Sequence[str], *, as_json: bool) -> int:
    """Parse ``opendj sync ratings`` flags, refuse an unacknowledged live run, then forward to the module."""
    parser = ratings_parser()
    if any(token in ("-h", "--help") for token in tokens):
        parser.print_help()
        return EXIT_CONFIRMED
    try:
        args = parser.parse_args(list(tokens))
    except ValueError as error:
        return fail(as_json, "usage", f"sync ratings: {error}", EXIT_FAILED)
    if args.live and not args.i_understand_the_risks:
        return fail(
            as_json,
            "acknowledgement_required",
            f"sync ratings --live writes Rekordbox and djay; re-run with {ACK_FLAG} after a dry run",
            EXIT_FAILED,
        )
    if not args.live and as_json:
        return _dry_run_json(args.diff_csv or default_diff_csv())
    from apps.sync import apply_ratings

    return apply_ratings.main(module_argv(args))


def run(tokens: Sequence[str], *, as_json: bool) -> int:
    """Dispatch ``opendj sync <subcommand>``; only ``ratings`` exists today."""
    if not tokens:
        print(SYNC_USAGE, file=sys.stderr)
        return EXIT_FAILED
    if tokens[0] in ("-h", "--help"):
        print(SYNC_USAGE)
        return EXIT_CONFIRMED
    head, rest = tokens[0], list(tokens[1:])
    if head == "ratings":
        return run_ratings(rest, as_json=as_json)
    return fail(as_json, "usage", f"unknown sync subcommand {head!r}; try `opendj sync --help`", EXIT_FAILED)


__all__ = ["SYNC_USAGE", "module_argv", "ratings_parser", "run", "run_ratings"]
