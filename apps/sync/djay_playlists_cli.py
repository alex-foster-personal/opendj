"""SYNC-01 CLI: enumerate djay playlists and track UUID memberships.

HTTP twin: ``GET /api/v1/rb-djay-sync/djay/playlists``.
"""
from __future__ import annotations

import argparse
import json
import sys

from apps.webui.server.rb_djay_sync_service import RbDjaySyncError, list_djay_playlists


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.sync djay-playlists",
        description="List djay Pro playlists from the working-copy MediaLibrary.db.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of a plain table",
    )
    args = parser.parse_args(argv)
    try:
        playlists = list_djay_playlists()
    except RbDjaySyncError as exc:
        print(f"[djay-playlists] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"playlists": playlists, "count": len(playlists)}, indent=2))
        return 0
    for pl in playlists:
        print(f"{pl['name']}\t{pl['uuid']}\t{pl['track_count']} tracks")
    print(f"[djay-playlists] total={len(playlists)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
