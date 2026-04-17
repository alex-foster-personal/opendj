"""``python -m apps.smartlists.refresh`` -- manual materialisation CLI.

Drives the Materializer via the same path the triggers use. Default is
dry-run; ``--live --i-understand-the-risks`` is required to write.

TODO(phase-3): once ``apps.sync.playlists.RBPlaylistWriter`` +
``DjayPlaylistWriter`` land, wire them up in :func:`_build_writers`.
Until then the CLI materialises against an empty writer list when
--live is provided, so the dry-run path stays fully testable.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apps.smartlists.cli._common import build_repo as build_smartlists_repo
from apps.smartlists.materializer import Materializer


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.smartlists.refresh",
        description="Re-materialise smartlists from the shared-state DB.",
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--name", default=None)
    scope.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--live", action="store_true")
    p.add_argument("--i-understand-the-risks", action="store_true")
    p.add_argument("--force-adopt", action="store_true")
    p.add_argument("--db", type=Path, default=None)
    return p


def _build_writers() -> list:
    """Return production playlist writers.

    Empty today; Phase 3 writers land here. See TODO in module docstring.
    """
    return []


def main(argv: list[str] | None = None, *, out=None) -> int:
    args = build_parser().parse_args(argv)
    stream = out if out is not None else sys.stdout

    if args.live and not args.i_understand_the_risks:
        print(
            "error: --live requires --i-understand-the-risks",
            file=sys.stderr,
        )
        return 2
    do_live = args.live and args.i_understand_the_risks
    do_dry = not do_live

    repo, conn = build_smartlists_repo(args.db)
    try:
        materializer = Materializer(repo, _build_writers())
        if args.name:
            row = repo.get_by_name(args.name)
            if row is None:
                print(f"error: no smartlist named {args.name!r}",
                      file=sys.stderr)
                return 1
            results = [
                materializer.materialize(
                    row.id, dry_run=do_dry, live=do_live,
                    force_adopt=args.force_adopt,
                )
            ]
        else:
            results = materializer.materialize_all(
                dry_run=do_dry, live=do_live,
                force_adopt=args.force_adopt,
            )
    finally:
        conn.close()

    _render_summary(results, stream, dry_run=do_dry)
    return 0 if all(r.ok for r in results) else 1


def _render_summary(results: list, stream, *, dry_run: bool) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except Exception:  # pragma: no cover
        stream.write("smartlist\tadded\tremoved\ttotal\twriters\tok\n")
        for r in results:
            stream.write(
                f"{r.smartlist_name}\t{len(r.added_tracks)}\t"
                f"{len(r.removed_tracks)}\t{r.total_tracks}\t"
                f"{r.writers_applied}\t{r.ok}\n"
            )
        if dry_run:
            stream.write("(dry-run: no writes applied)\n")
        return
    console = Console(file=stream, force_terminal=False)
    title = (
        "smartlist materialisation (dry-run)" if dry_run
        else "smartlist materialisation (LIVE)"
    )
    table = Table(title=title)
    for col in ("smartlist", "added", "removed", "total", "writers", "ok"):
        table.add_column(col)
    for r in results:
        table.add_row(
            r.smartlist_name,
            str(len(r.added_tracks)),
            str(len(r.removed_tracks)),
            str(r.total_tracks),
            ", ".join(
                f"{v}={'+' if ok else '-'}"
                for v, ok in r.writers_applied.items()
            ) or "-",
            "OK" if r.ok else "; ".join(r.errors),
        )
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
