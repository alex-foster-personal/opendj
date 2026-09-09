"""Operator actions on a sync hub or spoke, as functions and as a CLI.

    uv run python -m apps.sync_hub sync         --data-dir DIR --hub URL [--name N]
    uv run python -m apps.sync_hub generation   --data-dir DIR
    uv run python -m apps.sync_hub rotate       --data-dir DIR
    uv run python -m apps.sync_hub prune        --data-dir DIR [--changelog T]
                                                [--keep-days N] [--keep-rows N]

Four operations:

* **sync** runs one spoke round trip against ``--hub`` (round 3 finding R7).
  Nothing outside pytest called ``run_sync`` before -- the whole spoke
  protocol, and the retention prune it depends on, was library code with no
  operator or agent entry point. This is the agent-native-parity twin of the
  ``/cloudsync`` UI's own sync button.
* **prune** bounds a changelog that had no retention at all (round 2 finding
  N6) -- one row per synced-table write, forever. It only ever drops entries
  that a NEWER entry for the same row supersedes, so the pull loses no
  coverage and ``MAX(seq)`` cannot move.
* **rotate** re-mints this hub's generation token by hand, for the restore
  case the anchor cannot see on its own: a whole-machine restore rolls the
  data dir back too, so the anchor agrees with the DB and nothing looks
  wrong. Every spoke then re-offers its library once.
* **generation** prints the current token.

Every UI/daemon action in this repo has a CLI twin (the agent-native parity
rule); these are the twin the sync surface will match.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.sync_hub import client, engine, generation
from apps.sync_hub import status as sync_status


def _open(data_dir: Path) -> sqlite3.Connection:
    return state_db.open_rw(client.state_db_path(data_dir))


def sync(
    data_dir: Path,
    hub_url: str,
    *,
    name: str | None = None,
    transport: client.HubTransport | None = None,
) -> client.SyncResult:
    """Run one spoke round trip against ``hub_url``. The spoke's operator entry.

    ``transport`` is here for the tests that drive the real router in-process;
    the CLI never passes it, so an operator always talks real HTTP. Any of
    ``run_sync``'s declared failures (see its docstring) propagate out
    unchanged -- fail fast, no repair.
    """
    started_at = sync_stamp.canonical_now()
    try:
        result = client.run_sync(
            Path(data_dir), hub_url, transport=transport, name=name
        )
    except Exception as exc:
        sync_status.write_result(
            Path(data_dir),
            sync_status.SyncResult(
                finished_at=sync_stamp.canonical_now(),
                status="error",
                message=str(exc),
                pushed=0,
                pulled=0,
            ),
        )
        raise
    sync_status.write_result(
        Path(data_dir),
        sync_status.SyncResult(
            finished_at=sync_stamp.canonical_now(),
            status="ok",
            message=f"completed sync started at {started_at}",
            pushed=result.pushed,
            pulled=result.pulled,
        ),
    )
    return result


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

    sync_command = subcommands.add_parser(
        "sync", parents=[common], help="run one spoke round trip against a hub"
    )
    sync_command.add_argument(
        "--hub", required=True, help="the hub base URL, e.g. http://hub.tailnet:8686"
    )

    subcommands.add_parser(
        "status", parents=[common], help="print the CloudSync status object as JSON"
    )
    sync_command.add_argument(
        "--name",
        default=None,
        help="this machine's display name; defaults to the hostname",
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
    if args.command == "sync":
        result = sync(args.data_dir, args.hub, name=args.name)
        restored = ", hub restore detected" if result.hub_restore_detected else ""
        print(
            f"synced against hub {result.hub_machine_id}: pushed "
            f"{result.pushed} (accepted {result.accepted}, rejected "
            f"{result.rejected}), pulled {result.pulled} (applied "
            f"{result.applied}), {result.rounds} round(s), hub seq "
            f"{result.hub_seq}{restored}"
        )
    elif args.command == "status":
        current = sync_status.read_status(args.data_dir)
        print(json.dumps(current.to_wire(), sort_keys=True))
        if current.last_result is not None and current.last_result["status"] == "error":
            return 1
    elif args.command == "generation":
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


__all__ = ["main", "prune", "rotate", "show_generation", "sync"]
