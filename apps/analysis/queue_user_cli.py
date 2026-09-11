"""CLI verbs for user-ordered stems/lyrics jobs (issue #1865)."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from . import queue_user
from .queue_user_lanes import USER_JOB_LANES
from .store import open_conn


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    db = Path(args.db) if args.db else None
    conn_path = db
    if conn_path is None:
        from apps.shared.paths import STATE_DB

        conn_path = STATE_DB
    if conn_path.name == "state.db" and conn_path.parent.name == "state":
        data_dir = conn_path.parent.parent
    else:
        data_dir = Path(args.data_dir) if getattr(args, "data_dir", None) else conn_path.parent
    stems_root = (
        Path(args.stems_root)
        if getattr(args, "stems_root", None)
        else data_dir / "state" / "stems"
    )
    return data_dir, stems_root


def _conn(args: argparse.Namespace):
    conn = open_conn(Path(args.db) if args.db else None)
    queue_user.ensure_user_schema(conn)
    return conn


def _item_dict(item: queue_user.UserJobItem) -> dict[str, Any]:
    return {
        "lane": item.lane,
        "stable_id": item.stable_id,
        "state": item.state,
        "reason": item.reason,
        "detail": item.detail,
        "position": item.position,
        "attempts": item.attempts,
        "enqueued_at": item.enqueued_at,
        "started_at": item.started_at,
        "finished_at": item.finished_at,
    }


def cmd_user_enqueue(args: argparse.Namespace) -> int:
    from .queue_cli import EXIT_OK, _emit

    data_dir, stems_root = _paths(args)
    conn = _conn(args)
    try:
        result = queue_user.enqueue_next(
            conn,
            lane=args.lane,
            stable_ids=args.stable_id,
            placement=args.placement,
            stems_root=stems_root,
            data_dir=data_dir,
        )
    finally:
        conn.close()
    _emit(
        {
            "lane": result.lane,
            "items": [_item_dict(i) for i in result.items],
            "already_running": list(result.already_running),
        },
        as_json=args.json,
    )
    return EXIT_OK


def cmd_user_list(args: argparse.Namespace) -> int:
    from .queue_cli import EXIT_OK, _emit

    conn = _conn(args)
    try:
        items = queue_user.list_lane(
            conn, args.lane, include_settled=bool(args.include_settled)
        )
        counts = queue_user.counts_for_lane(conn, args.lane)
    finally:
        conn.close()
    _emit(
        {
            "lane": args.lane,
            "items": [_item_dict(i) for i in items],
            "counts": counts,
        },
        as_json=args.json,
    )
    return EXIT_OK


def cmd_user_reorder(args: argparse.Namespace) -> int:
    from .queue_cli import EXIT_OK, _emit

    conn = _conn(args)
    try:
        item = queue_user.reorder_item(
            conn,
            lane=args.lane,
            stable_id=args.stable_id,
            before_stable_id=args.before_stable_id,
        )
    finally:
        conn.close()
    _emit(_item_dict(item), as_json=args.json)
    return EXIT_OK


def cmd_user_cancel(args: argparse.Namespace) -> int:
    from .queue_cli import EXIT_OK, _emit

    conn = _conn(args)
    try:
        item = queue_user.cancel_item(
            conn, lane=args.lane, stable_id=args.stable_id
        )
    finally:
        conn.close()
    _emit(_item_dict(item), as_json=args.json)
    return EXIT_OK


def add_user_parsers(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("user-enqueue", help="enqueue stems/lyrics jobs at head or tail")
    p.add_argument("--lane", required=True, choices=USER_JOB_LANES)
    p.add_argument("--stable-id", action="append", required=True, dest="stable_id")
    p.add_argument("--placement", choices=("next", "tail"), default="next")
    p.add_argument("--stems-root")
    p.add_argument("--data-dir")
    p.set_defaults(func=cmd_user_enqueue)

    p = sub.add_parser("user-list", help="list a user job lane in queue order")
    p.add_argument("--lane", required=True, choices=USER_JOB_LANES)
    p.add_argument("--include-settled", action="store_true")
    p.set_defaults(func=cmd_user_list)

    p = sub.add_parser("user-reorder", help="move a pending user job")
    p.add_argument("--lane", required=True, choices=USER_JOB_LANES)
    p.add_argument("--stable-id", required=True)
    p.add_argument("--before-stable-id", default=None)
    p.set_defaults(func=cmd_user_reorder)

    p = sub.add_parser("user-cancel", help="cancel a pending or running user job")
    p.add_argument("--lane", required=True, choices=USER_JOB_LANES)
    p.add_argument("--stable-id", required=True)
    p.set_defaults(func=cmd_user_cancel)


# Re-export so a parity test can name this module.
__all__ = [
    "add_user_parsers",
    "cmd_user_cancel",
    "cmd_user_enqueue",
    "cmd_user_list",
    "cmd_user_reorder",
]
