"""``python -m apps.pairings.add`` -- insert one pairing.

Example::

    python -m apps.pairings.add \
        --from abc123... --to def456... \
        --direction into --notes "peak-hour transition"
"""
from __future__ import annotations

import argparse

from apps.shared.pairings import PairingsError
from apps.shared.pairings.models import DIRECTIONS, SOURCES

from ._common import add_db_argument
from ._session import pairing_repo, pairings_error_exit


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.pairings.add",
        description="Add a pairing edge between two tracks (by stable_id).",
    )
    p.add_argument("--from", dest="from_id", required=True,
                   help="stable_id of the source track")
    p.add_argument("--to", dest="to_id", required=True,
                   help="stable_id of the destination track")
    p.add_argument("--direction", default="either", choices=list(DIRECTIONS),
                   help="edge direction (default: either)")
    p.add_argument("--source", default="manual", choices=list(SOURCES),
                   help="provenance tag (default: manual)")
    p.add_argument("--notes", default=None,
                   help="free-text note describing the transition")
    p.add_argument("--confidence", type=float, default=None,
                   help="optional confidence score in [0,1]")
    add_db_argument(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with pairing_repo(args.db) as repo:
        try:
            edge = repo.add(
                args.from_id,
                args.to_id,
                direction=args.direction,
                source=args.source,
                notes=args.notes,
                confidence=args.confidence,
            )
        except PairingsError as exc:
            return pairings_error_exit(exc)
        print(
            f"added pairing {edge.from_stable_id} -> {edge.to_stable_id} "
            f"[{edge.direction}] source={edge.source}"
        )
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
