"""Pack/unpack a portable data snapshot for a Windows backend/test run.

``state/state.db`` never ships loose (it is always gitignored and derived
from the live vendor DBs), so a Windows box needs a deliberate, mapped
snapshot to run the webui backend or the test suite against real data
shapes without touching the Mac's live Rekordbox install.

Pipeline::

    pack --data-dir <dir> --out snapshot.tar
        bundles state/state.db + master.plain.db + state/vocal-cache/
        (NO audio bytes) and writes a generated path-map.json stub inside
        the tar whose "from" prefixes are the distinct absolute FolderPath
        roots read from master.plain.db.djmdContent, each with an empty
        "to" for the operator to fill in.

    unpack --snapshot snapshot.tar --dest <dir>
        extracts into a data dir the backend can point at via MDT_DATA_DIR.
        Fails fast if the tar is missing a required member.

Usage::

    python -m scripts.data_snapshot pack --data-dir data --out snapshot.tar
    python -m scripts.data_snapshot pack --data-dir data --out snapshot.tar --json
    python -m scripts.data_snapshot unpack --snapshot snapshot.tar --dest D:/mdt-data

Agent-native: pure CLI, no interactive prompts, no hidden defaults.
``--data-dir`` / ``--snapshot`` / ``--dest`` are always required.

-Claude
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import tarfile
from pathlib import Path
from typing import Any

# Members bundled into the tar, relative to --data-dir. No audio bytes ever
# (those ship per-batch, out of band).
STATE_DB_MEMBER: str = "state/state.db"
MASTER_PLAIN_DB_MEMBER: str = "master.plain.db"
VOCAL_CACHE_MEMBER: str = "state/vocal-cache"
PATH_MAP_MEMBER: str = "path-map.json"

REQUIRED_MEMBERS: tuple[str, ...] = (STATE_DB_MEMBER, MASTER_PLAIN_DB_MEMBER)


def _discover_folder_path_roots(master_plain_db: Path) -> list[str]:
    """Return the distinct absolute FolderPath top-level roots in ``djmdContent``.

    A "root" here is the first two path segments (e.g. ``/Users/dev``) so the
    generated path-map stub stays small and generically rewritable, rather
    than one entry per track. Streaming / empty FolderPaths are excluded.
    """
    conn = sqlite3.connect(f"file:{master_plain_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT FolderPath FROM djmdContent WHERE FolderPath IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    roots: set[str] = set()
    for (folder_path,) in rows:
        if not folder_path or not folder_path.startswith("/"):
            continue
        parts = folder_path.strip("/").split("/")
        if len(parts) < 2:
            continue
        roots.add("/" + "/".join(parts[:2]))
    return sorted(roots)


def _generated_path_map_json(roots: list[str]) -> bytes:
    payload = {"entries": [{"from": root, "to": ""} for root in roots]}
    return json.dumps(payload, indent=2).encode("utf-8")


def pack(data_dir: Path, out: Path) -> dict[str, Any]:
    """Bundle state.db + master.plain.db + vocal-cache into ``out``.

    Raises ``FileNotFoundError`` if state.db or master.plain.db is missing
    under ``data_dir`` -- fail fast, no partial snapshot.
    """
    state_db = data_dir / "state" / "state.db"
    master_plain_db = data_dir / "master.plain.db"
    vocal_cache_dir = data_dir / "state" / "vocal-cache"

    if not state_db.is_file():
        raise FileNotFoundError(f"required member missing: {state_db}")
    if not master_plain_db.is_file():
        raise FileNotFoundError(f"required member missing: {master_plain_db}")

    roots = _discover_folder_path_roots(master_plain_db)
    path_map_bytes = _generated_path_map_json(roots)

    members: list[dict[str, Any]] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w") as tar:
        tar.add(state_db, arcname=STATE_DB_MEMBER)
        members.append({"member": STATE_DB_MEMBER, "bytes": state_db.stat().st_size})

        tar.add(master_plain_db, arcname=MASTER_PLAIN_DB_MEMBER)
        members.append(
            {"member": MASTER_PLAIN_DB_MEMBER, "bytes": master_plain_db.stat().st_size}
        )

        if vocal_cache_dir.is_dir():
            tar.add(vocal_cache_dir, arcname=VOCAL_CACHE_MEMBER)
            cache_bytes = sum(f.stat().st_size for f in vocal_cache_dir.rglob("*") if f.is_file())
            members.append({"member": VOCAL_CACHE_MEMBER, "bytes": cache_bytes})
        # else: vocal cache is optional (worker output; may not exist yet).

        path_map_info = tarfile.TarInfo(name=PATH_MAP_MEMBER)
        path_map_info.size = len(path_map_bytes)
        tar.addfile(path_map_info, io.BytesIO(path_map_bytes))
        members.append({"member": PATH_MAP_MEMBER, "bytes": len(path_map_bytes)})

    return {
        "out": str(out),
        "members": members,
        "path_map_roots": roots,
    }


def unpack(snapshot: Path, dest: Path) -> dict[str, Any]:
    """Extract ``snapshot`` into ``dest``.

    Raises ``FileNotFoundError`` if the tar itself is missing, and
    ``ValueError`` if any :data:`REQUIRED_MEMBERS` entry is absent from the
    tar -- fail fast rather than leaving a silently incomplete data dir.
    """
    if not snapshot.is_file():
        raise FileNotFoundError(f"snapshot not found: {snapshot}")

    with tarfile.open(snapshot, "r") as tar:
        names = set(tar.getnames())
        missing = [m for m in REQUIRED_MEMBERS if m not in names]
        if missing:
            raise ValueError(f"snapshot {snapshot} is missing required members: {missing}")

        dest.mkdir(parents=True, exist_ok=True)
        tar.extractall(dest, filter="data")

    return {"dest": str(dest), "members": sorted(names)}


def _cmd_pack(args: argparse.Namespace) -> int:
    result = pack(Path(args.data_dir), Path(args.out))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"wrote {result['out']}")
        for member in result["members"]:
            print(f"  {member['member']}  ({member['bytes']} bytes)")
        print(f"path-map roots discovered: {result['path_map_roots']}")
    return 0


def _cmd_unpack(args: argparse.Namespace) -> int:
    result = unpack(Path(args.snapshot), Path(args.dest))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"extracted to {result['dest']}")
        for member in result["members"]:
            print(f"  {member}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.data_snapshot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pack_parser = subparsers.add_parser("pack", help="bundle a data dir into a snapshot tar")
    pack_parser.add_argument("--data-dir", required=True, help="data dir containing state/state.db + master.plain.db")
    pack_parser.add_argument("--out", required=True, help="output tar path")
    pack_parser.add_argument("--json", action="store_true", help="emit a JSON summary instead of text")
    pack_parser.set_defaults(func=_cmd_pack)

    unpack_parser = subparsers.add_parser("unpack", help="extract a snapshot tar into a data dir")
    unpack_parser.add_argument("--snapshot", required=True, help="snapshot tar path")
    unpack_parser.add_argument("--dest", required=True, help="destination data dir")
    unpack_parser.add_argument("--json", action="store_true", help="emit a JSON summary instead of text")
    unpack_parser.set_defaults(func=_cmd_unpack)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
