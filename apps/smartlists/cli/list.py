"""``python -m apps.smartlists.cli.list`` -- list configured smartlists."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ._common import build_repo


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.smartlists.cli.list",
        description="List configured smartlists.",
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
        payload = [
            {
                "id": r.id, "name": r.name, "rule": r.rule,
                "order_by": r.order_by,
                "referenced_fields": sorted(r.referenced_fields),
                "last_materialized_count": len(r.last_materialized_track_ids),
                "last_evaluated_at": (
                    r.last_evaluated_at.isoformat() if r.last_evaluated_at else None
                ),
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
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
