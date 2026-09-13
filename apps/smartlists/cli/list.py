"""``python -m apps.smartlists.cli.list`` -- list configured smartlists.

Documented agent path: ``opendj api GET /api/v1/smartlists``.
This module reads a local state.db (tests / offline).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ._common import build_repo, smartlist_json


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.smartlists.cli.list",
        description=(
            "List configured smartlists. "
            "Documented agent path: opendj api GET /api/v1/smartlists. "
            "This module reads a local state.db (tests / offline)."
        ),
    )
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--format", default="table", choices=["table", "json"])
    return p


def main(argv: list[str] | None = None, *, out=None) -> int:
    args = build_parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    repo, conn = build_repo(args.db)
    try:
        rows = list(repo.list_all())
    finally:
        conn.close()
    if args.format == "json":
        payload = [smartlist_json(row) for row in rows]
        json.dump(payload, stream, indent=2)
        stream.write("\n")
        return 0
    stream.write(f"{'name':<40} {'order_by':<20} fields\n")
    for r in rows:
        stream.write(
            f"{r.name:<40} {r.order_by:<20} {sorted(r.referenced_fields)}\n"
        )
    stream.write(f"({len(rows)} smartlists)\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
