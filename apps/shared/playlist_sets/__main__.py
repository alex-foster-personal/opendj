"""CLI for playlist sets (SET-05) and set routes (SET-6586).

``python -m apps.playlist_sets``: the same verbs the HTTP routes expose.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from apps.shared.paths import STATE_DB
from apps.shared.playlist_sets import (
    ComponentInUseError,
    create_component,
    create_playlist_set,
    delete_component,
    list_components,
    list_playlist_sets,
    list_routes,
    load_playlist_set,
    record_run,
    update_component,
    update_set_parts,
)
from apps.shared.playlist_sets.schema import apply_playlist_set_migrations
from apps.shared.state.migrated_open import open_rw_migrated


def _set_to_dict(ps) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": ps.id,
        "playlist_id": ps.playlist_id,
        "name": ps.name,
        "play_count": ps.play_count,
        "parts": list(ps.parts),
        "entries": [
            {"stable_id": e.stable_id, "position": e.position} for e in ps.entries
        ],
        "created_at": ps.created_at,
        "updated_at": ps.updated_at,
    }


def _component_to_dict(c) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": c.id,
        "playlist_id": c.playlist_id,
        "name": c.name,
        "slot": c.slot,
        "stable_ids": list(c.stable_ids),
        "tags": list(c.tags),
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _id_list(raw: str) -> list[int]:
    try:
        return [int(x) for x in _str_list(raw)]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected comma-separated ids, got {raw!r}") from exc


def _str_list(raw: str) -> list[str]:
    """Split on commas; an empty item (``a,,b`` or a trailing comma) is an error."""
    items = [x.strip() for x in raw.split(",")]
    if not all(items):
        raise argparse.ArgumentTypeError(f"empty item in comma-separated list {raw!r}")
    return items


def _add_component_parser(sub) -> None:  # type: ignore[no-untyped-def]
    comp = sub.add_parser("component", help="components: tagged runs of tracks")
    csub = comp.add_subparsers(dest="component_cmd", required=True)
    cl = csub.add_parser("list")
    cl.add_argument("--playlist", required=True)
    cc = csub.add_parser("create")
    cc.add_argument("--playlist", required=True)
    cc.add_argument("--name", required=True)
    cc.add_argument("--slot", required=True)
    cc.add_argument("--tracks", type=_str_list, required=True, help="comma-separated stable_ids")
    cc.add_argument("--tag", action="append", default=[], dest="tags")
    cu = csub.add_parser("update")
    cu.add_argument("--id", type=int, required=True, dest="component_id")
    cu.add_argument("--name")
    cu.add_argument("--slot")
    cu.add_argument("--tracks", type=_str_list)
    cu.add_argument("--tag", action="append", default=None, dest="tags")
    cu.add_argument("--clear-tags", action="store_true", help="remove every tag")
    cd = csub.add_parser("delete")
    cd.add_argument("--id", type=int, required=True, dest="component_id")


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
    cr.add_argument("--parts", type=_id_list, default=None, help="route: component ids")

    for verb in ("perform", "practice"):
        run = sub.add_parser(verb)
        run.add_argument("--set", type=int, required=True, dest="set_id")
        run.add_argument(
            "--played-parts", type=_id_list, default=None,
            help="the route actually played, when it differs from the set's",
        )

    sp = sub.add_parser("set-parts", help="replace a set's route")
    sp.add_argument("--set", type=int, required=True, dest="set_id")
    sp.add_argument("--parts", type=_id_list, required=True)

    rt = sub.add_parser("routes", help="every route through a playlist's components")
    rt.add_argument("--playlist", required=True)
    rt.add_argument("--limit", type=int, default=64)

    _add_component_parser(sub)
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
        parts=args.parts,
    )
    conn.commit()
    ps = load_playlist_set(conn, set_id)
    if args.as_json:
        print(json.dumps(_set_to_dict(ps)))
    else:
        print(f"created set {set_id} {ps.name}")
    return 0


def _cmd_record_run(conn, args, kind: str) -> int:
    play_count = record_run(conn, args.set_id, kind, played_parts=args.played_parts)
    conn.commit()
    if args.as_json:
        print(json.dumps({"set_id": args.set_id, "kind": kind, "play_count": play_count}))
    else:
        verb = "performed" if kind == "performance" else "practiced"
        print(f"{verb} set {args.set_id} play_count={play_count}")
    return 0


def _cmd_set_parts(conn, args) -> int:
    ps = update_set_parts(conn, args.set_id, args.parts)
    conn.commit()
    if args.as_json:
        print(json.dumps(_set_to_dict(ps)))
    else:
        print(f"set {ps.id} route {','.join(str(p) for p in ps.parts)}")
    return 0


def _cmd_routes(conn, args) -> int:
    listing = list_routes(conn, args.playlist, limit=args.limit)
    if args.as_json:
        print(json.dumps({
            "total": listing.total,
            "truncated": listing.truncated,
            "routes": [
                {
                    "parts": r.parts,
                    "stable_ids": r.stable_ids,
                    "set_id": r.set_id,
                    "repeats": r.repeats,
                }
                for r in listing.routes
            ],
        }))
    else:
        for r in listing.routes:
            saved = "-" if r.set_id is None else f"set {r.set_id}"
            repeats = f"\trepeats {','.join(r.repeats)}" if r.repeats else ""
            parts = ",".join(str(p) for p in r.parts)
            print(f"{parts}\t{len(r.stable_ids)} tracks\t{saved}{repeats}")
        suffix = f" (showing {len(listing.routes)})" if listing.truncated else ""
        print(f"{listing.total} routes{suffix}")
    return 0


def _component_list(conn, args) -> int:
    comps = list_components(conn, args.playlist)
    if args.as_json:
        print(json.dumps([_component_to_dict(c) for c in comps]))
    else:
        for c in comps:
            tags = " ".join(f"#{t}" for t in c.tags)
            print(f"{c.id}\t{c.slot}\t{c.name}\t{len(c.stable_ids)} tracks\t{tags}".rstrip())
    return 0


def _component_create(conn, args) -> int:
    cid = create_component(
        conn, args.playlist, args.name, args.slot, args.tracks, tags=args.tags
    )
    conn.commit()
    print(json.dumps({"id": cid}) if args.as_json else f"created component {cid}")
    return 0


def _component_update(conn, args) -> int:
    if args.clear_tags and args.tags:
        print("give either --tag or --clear-tags, not both", file=sys.stderr)
        return 2
    comp = update_component(
        conn,
        args.component_id,
        name=args.name,
        slot=args.slot,
        stable_ids=args.tracks,
        tags=[] if args.clear_tags else args.tags,
    )
    conn.commit()
    if args.as_json:
        print(json.dumps(_component_to_dict(comp)))
    else:
        print(f"updated component {comp.id}")
    return 0


def _component_delete(conn, args) -> int:
    delete_component(conn, args.component_id)
    conn.commit()
    print(json.dumps({"deleted": args.component_id}) if args.as_json
          else f"deleted component {args.component_id}")
    return 0


_COMPONENT_CMDS = {
    "list": _component_list,
    "create": _component_create,
    "update": _component_update,
    "delete": _component_delete,
}


def _cmd_component(conn, args) -> int:
    handler = _COMPONENT_CMDS.get(args.component_cmd)
    if handler is None:
        raise AssertionError(f"unhandled component command {args.component_cmd!r}")
    return handler(conn, args)


def _dispatch(conn, args) -> int:
    if args.cmd == "list":
        return _cmd_list(conn, args)
    if args.cmd == "create":
        return _cmd_create(conn, args)
    if args.cmd == "perform":
        return _cmd_record_run(conn, args, "performance")
    if args.cmd == "practice":
        return _cmd_record_run(conn, args, "practice")
    if args.cmd == "set-parts":
        return _cmd_set_parts(conn, args)
    if args.cmd == "routes":
        return _cmd_routes(conn, args)
    if args.cmd == "component":
        return _cmd_component(conn, args)
    raise AssertionError(f"unhandled command {args.cmd!r}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        conn = open_rw_migrated(args.db, apply_playlist_set_migrations)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return _dispatch(conn, args)
    except (LookupError, ValueError, sqlite3.IntegrityError, ComponentInUseError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
