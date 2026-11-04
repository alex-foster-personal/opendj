"""Read-only survey of ``djmdCue.Kind`` distribution (issue #4069).

Never decrypts, writes, or guesses slot letters from row order. Decrypt is a
separate human step via ``apps.shared.rekordbox_db.decrypt_to_plain``.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from apps.adapters.rekordbox.cue_kind import KIND_BY_SLOT, kind_to_slot


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(r[1]) == column for r in rows)


def _live_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    if _table_has_column(conn, "djmdCue", "rb_local_deleted"):
        sql = (
            "SELECT ContentID, Kind, InMsec, OutMsec, ActiveLoop, Comment "
            "FROM djmdCue WHERE COALESCE(rb_local_deleted, 0) = 0"
        )
    else:
        sql = (
            "SELECT ContentID, Kind, InMsec, OutMsec, ActiveLoop, Comment "
            "FROM djmdCue"
        )
    return conn.execute(sql).fetchall()


def _kinds_by_content(rows: list[tuple[Any, ...]]) -> dict[str, set[int]]:
    out: dict[str, set[int]] = defaultdict(set)
    for content_id, kind, *_rest in rows:
        if kind is None:
            continue
        out[str(content_id)].add(int(kind))
    return dict(out)


def _hot_kinds_for_track(kinds: set[int]) -> set[int]:
    return {k for k in kinds if 1 <= k <= 8}


def _is_contiguous_skipping_4(kinds: set[int]) -> bool:
    hot = _hot_kinds_for_track(kinds)
    if 4 in hot or len(hot) < 4:
        return False
    lo, hi = min(hot), max(hot)
    for i in range(lo, hi + 1):
        if i == 4:
            continue
        if i not in hot:
            return False
    return True


def _is_contiguous_including_4(kinds: set[int]) -> bool:
    hot = _hot_kinds_for_track(kinds)
    if 4 not in hot or len(hot) < 4:
        return False
    lo, hi = min(hot), max(hot)
    return all(i in hot for i in range(lo, hi + 1))


def _library_summary(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    per_kind: Counter[int] = Counter()
    kind_4_loop = 0
    kind_4_non_loop = 0
    kinds_by_content = _kinds_by_content(rows)
    both_4_and_9: set[str] = set()

    for _content_id, kind, _in_msec, out_msec, *_rest in rows:
        if kind is None:
            continue
        k = int(kind)
        per_kind[k] += 1
        if k == 4:
            if out_msec is not None and int(out_msec) > 0:
                kind_4_loop += 1
            else:
                kind_4_non_loop += 1

    for cid, kinds in kinds_by_content.items():
        if 4 in kinds and 9 in kinds:
            both_4_and_9.add(cid)

    skip_4 = sum(
        1 for kinds in kinds_by_content.values() if _is_contiguous_skipping_4(kinds)
    )
    include_4 = sum(
        1
        for kinds in kinds_by_content.values()
        if _is_contiguous_including_4(kinds)
    )

    return {
        "live_rows_per_kind": {str(k): v for k, v in sorted(per_kind.items())},
        "tracks_with_ge4_kinds_contiguous_skipping_4": skip_4,
        "tracks_with_ge4_kinds_contiguous_including_4": include_4,
        "tracks_with_kind_4_and_kind_9": len(both_4_and_9),
        "kind_4_loop_count": kind_4_loop,
        "kind_4_non_loop_count": kind_4_non_loop,
        "mapping_in_use": {
            **KIND_BY_SLOT,
            "status": "unverified-for-D-H",
        },
    }


def _row_payload(
    kind: int,
    in_msec: Any,
    out_msec: Any,
    active_loop: Any,
    comment: Any,
) -> dict[str, Any]:
    comment_str = comment if comment is not None else ""
    return {
        "kind": int(kind),
        "in_msec": int(in_msec) if in_msec is not None else None,
        "out_msec": int(out_msec) if out_msec is not None else None,
        "active_loop": bool(active_loop) if active_loop is not None else False,
        "comment_len": len(str(comment_str)),
        "current_app_slot": kind_to_slot(int(kind)),
        "current_app_slot_status": "unverified",
    }


def _filter_content_ids(
    conn: sqlite3.Connection,
    _rows: list[tuple[Any, ...]],
    *,
    content_id: str | None,
    title_like: str | None,
) -> set[str] | None:
    if content_id is not None:
        return {content_id}
    if title_like is None:
        return None
    pattern = f"%{title_like}%"
    ids = conn.execute(
        "SELECT ID FROM djmdContent WHERE Commnt LIKE ? OR ID LIKE ?",
        (pattern, pattern),
    ).fetchall()
    return {str(r[0]) for r in ids}


def _track_details(
    conn: sqlite3.Connection,
    rows: list[tuple[Any, ...]],
    allowed: set[str] | None,
) -> list[dict[str, Any]]:
    by_content: dict[str, list[dict[str, Any]]] = defaultdict(list)
    titles: dict[str, str | None] = {}

    for content_id, kind, in_msec, out_msec, active_loop, comment in rows:
        cid = str(content_id)
        if allowed is not None and cid not in allowed:
            continue
        if kind is None:
            continue
        by_content[cid].append(
            _row_payload(kind, in_msec, out_msec, active_loop, comment)
        )

    for cid in by_content:
        try:
            row = conn.execute(
                "SELECT Commnt FROM djmdContent WHERE ID = ?", (cid,)
            ).fetchone()
            titles[cid] = str(row[0]) if row and row[0] is not None else None
        except sqlite3.Error:
            titles[cid] = None

    tracks: list[dict[str, Any]] = []
    for cid in sorted(by_content):
        entry: dict[str, Any] = {"content_id": cid, "rows": by_content[cid]}
        if cid in titles:
            entry["title"] = titles[cid]
        tracks.append(entry)
    return tracks


def _format_text(payload: dict[str, Any]) -> str:
    lines = ["# djmdCue Kind probe (issue #4069)", ""]
    for key, value in payload.items():
        if key == "tracks":
            continue
        lines.append(f"{key}: {value}")
    for track in payload.get("tracks") or []:
        lines.append("")
        lines.append(f"track {track['content_id']} title={track.get('title')!r}")
        lines.extend(f"  {row}" for row in track["rows"])
    return "\n".join(lines) + "\n"


def run_probe(
    db_path: Path,
    *,
    content_id: str | None = None,
    title_like: str | None = None,
) -> dict[str, Any]:
    conn = _open_readonly(db_path)
    try:
        rows = _live_rows(conn)
        payload = _library_summary(rows)
        allowed = _filter_content_ids(
            conn, rows, content_id=content_id, title_like=title_like
        )
        if content_id is not None or title_like is not None:
            payload["tracks"] = _track_details(conn, rows, allowed)
        return payload
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only djmdCue Kind survey for issue #4069"
    )
    parser.add_argument("--db", required=True, type=Path, help="Path to master.plain.db")
    parser.add_argument("--content-id", default=None, help="Filter to one ContentID")
    parser.add_argument("--title-like", default=None, help="Filter by djmdContent title substring")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format (default json)",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    if not args.db.is_file():
        print(f"database not readable: {args.db}", file=sys.stderr)
        return 2

    try:
        payload = run_probe(
            args.db,
            content_id=args.content_id,
            title_like=args.title_like,
        )
    except (sqlite3.Error, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.format == "text":
        sys.stdout.write(_format_text(payload))
    else:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
