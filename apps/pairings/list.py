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
from typing import TextIO

from apps.shared.pairings.models import SOURCES, PairingEdge

from ._common import add_db_argument
from ._emit import COLUMNS, edge_row_values
from ._session import pairing_repo


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
    add_db_argument(p)
    return p


def _emit_csv(edges: Iterable[PairingEdge], out: TextIO) -> None:
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for edge in edges:
        writer.writerow(
            edge_row_values(
                edge,
                truncate_ids=False,
                confidence_precision=3,
                modified_timespec=None,
            )
        )


def _emit_json(edges: Iterable[PairingEdge], out: TextIO) -> None:
    json.dump([e.as_dict() for e in edges], out, indent=2)
    out.write("\n")


def _emit_table(edges: list[PairingEdge], out: TextIO) -> None:
    # rich is optional; fall back to plain text if unavailable.
    try:
        from rich.console import Console
        from rich.table import Table
    except Exception:  # pragma: no cover - rich is in requirements.txt
        header = "\t".join(COLUMNS)
        out.write(header + "\n")
        for edge in edges:
            row = edge_row_values(
                edge,
                truncate_ids=True,
                confidence_precision=2,
                modified_timespec="seconds",
            )
            out.write("\t".join(row) + "\n")
        return
    console = Console(file=out, force_terminal=False)
    table = Table(title=f"pairings ({len(edges)})")
    for col in COLUMNS:
        table.add_column(col)
    for edge in edges:
        table.add_row(
            *edge_row_values(
                edge,
                truncate_ids=False,
                confidence_precision=2,
                modified_timespec="seconds",
            )
        )
    console.print(table)


def main(argv: list[str] | None = None, *, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    with pairing_repo(args.db) as repo:
        edges = list(repo.list_all(source=args.source, from_id=args.from_id))
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
