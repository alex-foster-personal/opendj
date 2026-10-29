"""``python -m apps.pairings.remove`` -- delete one pairing edge."""
from __future__ import annotations

import argparse
import sys

from apps.shared.pairings import PairingsError
from apps.shared.pairings.models import DIRECTIONS

from ._common import add_db_argument
from ._session import pairing_repo, pairings_error_exit


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.pairings.remove",
        description="Remove a pairing edge.",
    )
    p.add_argument("--from", dest="from_id", required=True)
    p.add_argument("--to", dest="to_id", required=True)
    p.add_argument("--direction", required=True, choices=list(DIRECTIONS))
    add_db_argument(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with pairing_repo(args.db) as repo:
        try:
            removed = repo.remove(args.from_id, args.to_id, args.direction)
        except PairingsError as exc:
            return pairings_error_exit(exc)
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
