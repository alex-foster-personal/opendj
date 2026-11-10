"""argparse surface for ``python -m apps.vocals`` (split from ``apps.vocals.cli``).

Handlers are injected by ``apps.vocals.cli.build_parser`` so this module never
imports the cli module back.

-Claude
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from pathlib import Path

from apps.shared.argparse_types import nonnegative_float as _nonnegative_float
from apps.shared.argparse_types import nonnegative_int as _nonnegative_int
from apps.shared.paths import DATA_DIR
from apps.vocals.cli_worker import WORKER_TIMEOUT_S

# ----- CFG ---------------------------------------------------------------------
DEFAULT_TRICKLE_LIMIT: int = 5


def build_vocals_parser(
    handlers: Mapping[str, Callable[[argparse.Namespace], int]],
) -> argparse.ArgumentParser:
    """The vocals argparse surface; ``handlers`` maps subcommand to its function."""
    parser = argparse.ArgumentParser(
        prog="python -m apps.vocals",
        description="demucs vocal-region gap-fill: scan coverage, trickle "
        "the todo queue, analyse one track, or backfill from stems.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help=f"data root holding state/state.db, master.plain.db and state/vocal-cache (default: {DATA_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[common], help="report vocal coverage + trickle ETA")
    scan.add_argument("--playlist", default=None, help="limit to one playlist name")
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    scan.set_defaults(func=handlers["scan"])

    trickle = sub.add_parser(
        "trickle",
        parents=[common],
        help="process the todo queue (requires --dry-run or --live)",
    )
    trickle.add_argument(
        "--limit",
        type=_nonnegative_int,
        default=DEFAULT_TRICKLE_LIMIT,
        help=f"max tracks started this run (default {DEFAULT_TRICKLE_LIMIT})",
    )
    trickle.add_argument(
        "--max-minutes",
        type=_nonnegative_float,
        default=None,
        help="wall-clock budget for this run in minutes; omit for unlimited",
    )
    trickle.add_argument(
        "--demucs-even-if-stems",
        action="store_true",
        help="include stem-derivable tracks in the demucs queue (default: leave them to from-stems)",
    )
    trickle.add_argument("--playlist", default=None, help="limit the queue to one playlist name")
    trickle.add_argument(
        "--worker-timeout-s",
        type=float,
        default=WORKER_TIMEOUT_S,
        help=f"per-track worker deadline in seconds (default {WORKER_TIMEOUT_S:g})",
    )
    mode = trickle.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="plan only, no worker runs")
    mode.add_argument(
        "--live",
        action="store_true",
        help="actually run the demucs worker + write cache",
    )
    trickle.set_defaults(func=handlers["trickle"])

    one = sub.add_parser("one", parents=[common], help="analyse a single track (debug path)")
    one.add_argument("--stable-id", required=True)
    one.add_argument(
        "--force",
        action="store_true",
        help="recompute even when a valid cache entry exists",
    )
    one.add_argument(
        "--worker-timeout-s",
        type=float,
        default=WORKER_TIMEOUT_S,
        help=f"worker deadline in seconds (default {WORKER_TIMEOUT_S:g})",
    )
    one.set_defaults(func=handlers["one"])

    from_stems = sub.add_parser(
        "from-stems",
        parents=[common],
        help="CPU backfill vocal-cache from existing stem bundles (no demucs)",
    )
    from_stems.add_argument(
        "--stable-id",
        default=None,
        help="only this track (default: every loadable stem bundle)",
    )
    from_stems.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max tracks to write this run (default: no cap)",
    )
    from_stems.add_argument(
        "--force",
        action="store_true",
        help="rewrite even when a valid vocal-cache entry exists",
    )
    fs_mode = from_stems.add_mutually_exclusive_group(required=True)
    fs_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="plan only, no cache writes",
    )
    fs_mode.add_argument(
        "--live",
        action="store_true",
        help="derive regions from stems and write vocal-cache",
    )
    from_stems.set_defaults(func=handlers["from-stems"])
    return parser
