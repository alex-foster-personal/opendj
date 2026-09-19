"""CLI for playlist sets (SET-05): ``python -m apps.shared.playlist_sets``."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from apps.shared.paths import STATE_DB
from apps.shared.playlist_sets import (
    create_playlist_set,
    list_playlist_sets,
    load_playlist_set,
    record_run,
)
from apps.shared.playlist_sets.schema import apply_playlist_set_migrations
from apps.shared.state import db as state_db


def _open_conn(db: Path) -> sqlite3.Connection:
    conn = state_db.open_rw(db)
    apply_playlist_set_migrations(conn)
    return conn


def _set_to_dict(ps) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": ps.id,
        "playlist_id": ps.playlist_id,
        "name": ps.name,
        "play_count": ps.play_count,
        "entries": [
            {"stable_id": e.stable_id, "position": e.position} for e in ps.entries
        ],
        "created_at": ps.created_at,
        "updated_at": ps.updated_at,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="playlist-sets", description=__doc__)
    p.add_argument("--db", type=Path, default=STATE_DB)
    p.add_argument("--json", action="store_true", dest="as_json")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list")
    ls.add_argument("--playlist", required=True)

    cr = sub.add_parser("create")
    cr.add_argument("--playlist", required=True)
    cr.add_argument("--name", required=True)
    cr.add_argument("--from-play-order", default=None)

    perf = sub.add_parser("perform")
    perf.add_argument("--set", type=int, required=True, dest="set_id")

    prac = sub.add_parser("practice")
    prac.add_argument("--set", type=int, required=True, dest="set_id")
    return p


def _cmd_list(conn, args) -> int:
    sets = list_playlist_sets(conn, args.playlist)
    if args.as_json:
        print(json.dumps([_set_to_dict(s) for s in sets]))
    else:
        for s in sets:
            print(f"{s.name}\t{s.play_count}")
    return 0


def _cmd_create(conn, args) -> int:
    set_id = create_playlist_set(
        conn,
        args.playlist,
        args.name,
        from_play_order=args.from_play_order,
    )
    conn.commit()
    ps = load_playlist_set(conn, set_id)
    if args.as_json:
        print(json.dumps(_set_to_dict(ps)))
    else:
        print(f"created set {set_id} {ps.name}")
    return 0


def _cmd_record_run(conn, args, kind: str) -> int:
    play_count = record_run(conn, args.set_id, kind)
    conn.commit()
    if args.as_json:
        print(json.dumps({"set_id": args.set_id, "kind": kind, "play_count": play_count}))
    else:
        verb = "performed" if kind == "performance" else "practiced"
        print(f"{verb} set {args.set_id} play_count={play_count}")
    return 0


def _dispatch(conn, args) -> int:
    if args.cmd == "list":
        return _cmd_list(conn, args)
    if args.cmd == "create":
        return _cmd_create(conn, args)
    if args.cmd == "perform":
        return _cmd_record_run(conn, args, "performance")
    if args.cmd == "practice":
        return _cmd_record_run(conn, args, "practice")
    raise AssertionError(f"unhandled command {args.cmd!r}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        conn = _open_conn(args.db)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return _dispatch(conn, args)
    except (LookupError, ValueError, sqlite3.IntegrityError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
