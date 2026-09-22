"""``python -m apps.pairings.list`` -- print the pairing graph.

Supports ``--format table|csv|json``. ``table`` uses ``rich`` if present
and falls back to a plain-text table so the CLI runs even without rich.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from apps.shared.pairings.models import SOURCES, PairingEdge

from ._common import build_repo


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.pairings.list",
        description="List pairing edges, optionally filtered.",
    )
    p.add_argument("--from", dest="from_id", default=None,
                   help="only list edges starting from this stable_id")
    p.add_argument("--source", default=None, choices=list(SOURCES),
                   help="filter by provenance")
    p.add_argument("--format", default="table",
                   choices=["table", "csv", "json"])
    p.add_argument("--db", type=Path, default=None)
    return p


_COLUMNS: tuple[str, ...] = (
    "from_stable_id",
    "to_stable_id",
    "direction",
    "source",
    "notes",
    "confidence",
    "modified_at",
)


def _emit_csv(edges: Iterable[PairingEdge], out) -> None:
    writer = csv.writer(out)
    writer.writerow(_COLUMNS)
    for e in edges:
        writer.writerow([
            e.from_stable_id,
            e.to_stable_id,
            e.direction,
            e.source,
            e.notes or "",
            "" if e.confidence is None else f"{e.confidence:.3f}",
            e.modified_at.isoformat(),
        ])


def _emit_json(edges: Iterable[PairingEdge], out) -> None:
    json.dump([e.as_dict() for e in edges], out, indent=2)
    out.write("\n")


def _emit_table(edges: list[PairingEdge], out) -> None:
    # rich is optional; fall back to plain text if unavailable.
    try:
        from rich.console import Console
        from rich.table import Table
    except Exception:  # pragma: no cover - rich is in requirements.txt
        header = "\t".join(_COLUMNS)
        out.write(header + "\n")
        for e in edges:
            row = [
                e.from_stable_id[:10] + "...",
                e.to_stable_id[:10] + "...",
                e.direction,
                e.source,
                e.notes or "",
                "" if e.confidence is None else f"{e.confidence:.2f}",
                e.modified_at.isoformat(timespec="seconds"),
            ]
            out.write("\t".join(row) + "\n")
        return
    console = Console(file=out, force_terminal=False)
    table = Table(title=f"pairings ({len(edges)})")
    for col in _COLUMNS:
        table.add_column(col)
    for e in edges:
        table.add_row(
            e.from_stable_id,
            e.to_stable_id,
            e.direction,
            e.source,
            e.notes or "",
            "" if e.confidence is None else f"{e.confidence:.2f}",
            e.modified_at.isoformat(timespec="seconds"),
        )
    console.print(table)


def main(argv: list[str] | None = None, *, out=None) -> int:
    args = build_parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    repo, conn = build_repo(args.db)
    try:
        edges = list(repo.list_all(source=args.source, from_id=args.from_id))
    finally:
        conn.close()
    if args.format == "csv":
        buf = io.StringIO()
        _emit_csv(edges, buf)
        stream.write(buf.getvalue())
    elif args.format == "json":
        _emit_json(edges, stream)
    else:
        _emit_table(edges, stream)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
