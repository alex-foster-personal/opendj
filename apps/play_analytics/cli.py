"""CLI parity for the play-analytics HTTP read model."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from apps.sets.paths import SETS_DB

from .query import AnalyticsSchemaError, query_play_analytics


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.play_analytics")
    parser.add_argument("--db", type=Path, default=SETS_DB)
    parser.add_argument(
        "--share-state",
        choices=("private", "shared_local", "shared_cloud"),
        default=None,
    )
    parser.add_argument("--limit", type=int, choices=range(1, 201), default=50)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = query_play_analytics(
            args.db,
            share_state=args.share_state,
            limit=args.limit,
        )
    except AnalyticsSchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


__all__ = ["main"]
