"""``python -m apps.lyrics fetch <stable_id>`` line-level lyrics CLI."""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from apps.lyrics.service import LyricsService
from apps.shared.paths import DATA_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.lyrics")
    subcommands = parser.add_subparsers(dest="command", required=True)
    fetch = subcommands.add_parser("fetch", help="fetch and cache line-synced lyrics")
    fetch.add_argument("track", help="stable track id")
    fetch.add_argument("--data-dir", type=Path, default=DATA_DIR, help="data root")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fetch":
        lyrics = LyricsService(args.data_dir).fetch_stable_id(args.track)
        print(json.dumps({
            "stable_id": lyrics.stable_id,
            "source": lyrics.source,
            "lines": [{"start_ms": line.start_ms, "text": line.text} for line in lyrics.lines],
        }, ensure_ascii=False))
        return 0
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
