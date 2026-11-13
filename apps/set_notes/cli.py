"""CLI parity for the set-notes HTTP endpoints (SETNOTE-6340c).

  python -m apps.set_notes list --data-dir PATH
  python -m apps.set_notes get scratch --data-dir PATH
  python -m apps.set_notes put scratch --body-file note.md --base-rev 3 --data-dir PATH

``put`` keeps the stored pad geometry and visibility; only the body changes.
Exit codes: 0 ok, 1 no note for the scope, 2 bad input or missing state.db,
3 stale ``--base-rev`` (stderr carries the current rev, as the HTTP 409 does).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from apps.shared.argparse_types import nonnegative_int
from apps.shared.paths import DATA_DIR

from .connection import open_set_notes
from .models import Geometry
from .revision import StaleRevision, stale_rev_detail
from .store import list_set_notes, load_set_note, save_set_note
from .wire import note_to_wire


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.set_notes")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="data root holding state/state.db (worktrees pass the primary's)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", parents=[common], help="list saved scopes")
    get = sub.add_parser("get", parents=[common], help="print one note as JSON")
    get.add_argument("scope")
    put = sub.add_parser("put", parents=[common], help="write a note body")
    put.add_argument("scope")
    put.add_argument("--body-file", type=Path, required=True, help="markdown file, '-' for stdin")
    put.add_argument("--base-rev", type=nonnegative_int, required=True, help="rev you edited; 0 creates")
    return parser


def _emit(payload: object, stream: TextIO | None = None) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True), file=stream)


def _read_body(body_file: Path) -> str:
    if str(body_file) == "-":
        return sys.stdin.read()
    return body_file.read_text(encoding="utf-8")


def _put(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, object]:
    body = _read_body(args.body_file)
    try:
        prior = load_set_note(conn, args.scope)
        geometry, visible = prior.geometry, prior.visible
    except LookupError:
        geometry, visible = Geometry(), False
    note = save_set_note(conn, args.scope, body, geometry, visible, args.base_rev)
    conn.commit()
    return note_to_wire(note)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        conn = open_set_notes(args.data_dir / "state" / "state.db")
    except FileNotFoundError as exc:
        _emit({"error": "state_db_missing", "message": str(exc)}, sys.stderr)
        return 2
    try:
        if args.command == "list":
            _emit([
                {"scope": n.scope, "rev": n.rev, "updated_at": n.updated_at, "chars": len(n.body_md)}
                for n in list_set_notes(conn)
            ])
        elif args.command == "get":
            _emit(note_to_wire(load_set_note(conn, args.scope)))
        else:
            _emit(_put(conn, args))
    except StaleRevision as exc:
        conn.rollback()
        _emit(stale_rev_detail(exc.current_rev), sys.stderr)
        return 3
    except LookupError:
        _emit({"error": "not_found", "scope": args.scope}, sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        _emit({"error": "invalid", "message": str(exc)}, sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


__all__ = ["main"]
