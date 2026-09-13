"""``python -m apps.smartlists.cli.delete`` -- delete a smartlist by name.

Documented agent path: ``opendj api DELETE /api/v1/smartlists/{id}``.
This module writes a local state.db (tests / offline).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._common import build_repo


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.smartlists.cli.delete",
        description=(
            "Delete a smartlist by name. "
            "Documented agent path: opendj api DELETE /api/v1/smartlists/{id}. "
            "This module writes a local state.db (tests / offline)."
        ),
    )
    p.add_argument("--name", required=True)
    p.add_argument("--db", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo, conn = build_repo(args.db)
    try:
        removed = repo.delete_by_name(args.name)
        if removed:
            print(f"deleted smartlist {args.name!r}")
            return 0
        print(f"no smartlist named {args.name!r}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
