"""``python -m apps.pairings.remove`` -- delete one pairing edge."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apps.shared.pairings import PairingsError
from apps.shared.pairings.models import DIRECTIONS

from ._common import build_repo


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.pairings.remove",
        description="Remove a pairing edge.",
    )
    p.add_argument("--from", dest="from_id", required=True)
    p.add_argument("--to", dest="to_id", required=True)
    p.add_argument("--direction", required=True, choices=list(DIRECTIONS))
    p.add_argument("--db", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo, conn = build_repo(args.db)
    try:
        try:
            removed = repo.remove(args.from_id, args.to_id, args.direction)
        except PairingsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if removed:
            print(
                f"removed pairing {args.from_id} -> {args.to_id} [{args.direction}]"
            )
            return 0
        print(
            f"no pairing {args.from_id} -> {args.to_id} [{args.direction}]",
            file=sys.stderr,
        )
        return 1
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
