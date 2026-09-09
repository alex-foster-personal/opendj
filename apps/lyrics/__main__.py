"""Line-level lyrics CLI: ``fetch`` one track, or build/resume the index.

``python -m apps.lyrics fetch <stable_id>`` fetches and caches one track's
line-synced lyrics (LYRICS-01). ``python -m apps.lyrics index`` builds or
resumes the durable checkpointed lyric-search index (LYRICS-03) by draining
one bounded batch at a time until nothing is left to index; ``--once`` runs
a single batch and reports, which is what a caller drives when it wants to
control pausing itself. ``--force-rebuild`` is the reachable recovery path
for the bulk-removal guard (issue #1343 round-3 review,
``apps.lyrics.search_index.MAX_REMOVAL_FRACTION``): the background watcher
never rebuilds on its own (that would defeat the guard), so an operator who
has confirmed a shrunk cache is real, not a mount/permissions problem, runs
this flag once to let the batch through.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from apps.lyrics.search_index import index_batch, index_path
from apps.lyrics.service import LyricsService
from apps.shared.paths import DATA_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.lyrics")
    subcommands = parser.add_subparsers(dest="command", required=True)

    fetch = subcommands.add_parser("fetch", help="fetch and cache line-synced lyrics")
    fetch.add_argument("track", help="stable track id")
    fetch.add_argument("--data-dir", type=Path, default=DATA_DIR, help="data root")

    index = subcommands.add_parser(
        "index", help="build or resume the checkpointed lyric-search index"
    )
    index.add_argument("--data-dir", type=Path, default=DATA_DIR, help="data root")
    index.add_argument(
        "--once",
        action="store_true",
        help="run one bounded batch and report, instead of draining to idle",
    )
    index.add_argument(
        "--batch",
        type=int,
        default=100,
        help="max documents indexed per batch (one poll of the job)",
    )
    index.add_argument(
        "--interval",
        type=float,
        default=0.2,
        help="seconds to pause between batches while draining",
    )
    index.add_argument(
        "--force-rebuild",
        action="store_true",
        help=(
            "reconcile a bulk removal (more than MAX_REMOVAL_FRACTION of the "
            "indexed rows gone) that the guard would otherwise refuse; only "
            "the first batch of this run applies it, so a real bulk removal "
            "does not disable the guard for the rest of the drain"
        ),
    )
    return parser


def _report(args: argparse.Namespace, *, done: bool, last_docs_indexed: int) -> int:
    print(
        json.dumps(
            {
                "indexed_file": str(index_path(args.data_dir)),
                "done": done,
                "docs_indexed": last_docs_indexed,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fetch":
        lyrics = LyricsService(args.data_dir).fetch_stable_id(args.track)
        print(
            json.dumps(
                {
                    "stable_id": lyrics.stable_id,
                    "source": lyrics.source,
                    "lines": [
                        {"start_ms": line.start_ms, "text": line.text} for line in lyrics.lines
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "index":
        batch = index_batch(args.data_dir, max_docs=args.batch, force_rebuild=args.force_rebuild)
        while not args.once and not batch.done:
            time.sleep(max(0.0, args.interval))
            # force_rebuild applies only to the first batch above: the bulk
            # removal it was for is reconciled by then, and re-arming the
            # guard for the rest of the drain is what keeps a real future
            # bulk removal from sailing through unnoticed.
            batch = index_batch(args.data_dir, max_docs=args.batch)
        return _report(args, done=batch.done, last_docs_indexed=batch.docs_indexed)
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
