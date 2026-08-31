"""Operator actions on a sync hub or spoke, as functions and as a CLI.

    uv run python -m apps.sync_hub generation   --data-dir DIR
    uv run python -m apps.sync_hub rotate       --data-dir DIR
    uv run python -m apps.sync_hub prune        --data-dir DIR [--changelog T]
                                                [--keep-days N] [--keep-rows N]

Two operations, both from round 2 finding N6:

* **prune** bounds a changelog that had no retention at all -- one row per
  synced-table write, forever. It only ever drops entries that a NEWER entry
  for the same row supersedes, so the pull loses no coverage and ``MAX(seq)``
  cannot move.
* **rotate** re-mints this hub's generation token by hand, for the restore
  case the anchor cannot see on its own: a whole-machine restore rolls the
  data dir back too, so the anchor agrees with the DB and nothing looks
  wrong. Every spoke then re-offers its library once.

Every UI/daemon action in this repo has a CLI twin (the agent-native parity
rule); these two have no UI yet, and this is the twin they will match.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from apps.shared.state import db as state_db
from apps.sync_hub import client, engine, generation


def _open(data_dir: Path) -> sqlite3.Connection:
    return state_db.open_rw(client.state_db_path(data_dir))


def prune(
    data_dir: Path,
    *,
    changelog: str = engine.HUB_CHANGELOG_TABLE,
    keep_days: float = engine.DEFAULT_KEEP_DAYS,
    keep_rows: int = engine.DEFAULT_KEEP_ROWS,
) -> int:
    """Prune one changelog in ``data_dir``'s state DB. Returns rows deleted."""
    conn = _open(data_dir)
    try:
        conn.execute("BEGIN")
        try:
            deleted = engine.prune_changelog(
                conn, changelog=changelog, keep_days=keep_days, keep_rows=keep_rows
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return deleted
    finally:
        conn.close()


def rotate(data_dir: Path) -> str:
    """Re-mint the hub generation token. Returns the new token."""
    conn = _open(data_dir)
    try:
        return generation.rotate(data_dir, seq=engine.current_seq(conn))
    finally:
        conn.close()


def show_generation(data_dir: Path) -> str:
    """This hub's current token, minting or rotating it as ``hello`` would."""
    conn = _open(data_dir)
    try:
        return generation.observe(conn, data_dir)
    finally:
        conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.sync_hub", description=__doc__.splitlines()[0]
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="the data dir holding state/state.db and machine-id",
    )

    subcommands.add_parser(
        "generation", parents=[common], help="print this hub's generation token"
    )
    subcommands.add_parser(
        "rotate",
        parents=[common],
        help="re-mint the token; every spoke re-offers its library once",
    )
    prune_command = subcommands.add_parser(
        "prune", parents=[common], help="drop superseded changelog entries"
    )
    prune_command.add_argument(
        "--changelog",
        default=engine.HUB_CHANGELOG_TABLE,
        choices=sorted(engine.CHANGELOG_TABLES),
    )
    prune_command.add_argument(
        "--keep-days", type=float, default=engine.DEFAULT_KEEP_DAYS
    )
    prune_command.add_argument("--keep-rows", type=int, default=engine.DEFAULT_KEEP_ROWS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand. Returns a process exit code."""
    args = _parser().parse_args(argv)
    if args.command == "generation":
        print(show_generation(args.data_dir))
    elif args.command == "rotate":
        print(rotate(args.data_dir))
    elif args.command == "prune":
        deleted = prune(
            args.data_dir,
            changelog=args.changelog,
            keep_days=args.keep_days,
            keep_rows=args.keep_rows,
        )
        print(f"pruned {deleted} superseded {args.changelog} entries")
    else:
        raise AssertionError(f"unhandled command {args.command!r}")
    return 0


__all__ = ["main", "prune", "rotate", "show_generation"]
