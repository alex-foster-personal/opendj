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
import stat
import sys
import tarfile
from pathlib import Path
from typing import Any, BinaryIO

# Members bundled into the tar, relative to --data-dir. No audio bytes ever
# (those ship per-batch, out of band).
STATE_DB_MEMBER: str = "state/state.db"
MASTER_PLAIN_DB_MEMBER: str = "master.plain.db"
VOCAL_CACHE_MEMBER: str = "state/vocal-cache"
PATH_MAP_MEMBER: str = "path-map.json"

REQUIRED_MEMBERS: tuple[str, ...] = (STATE_DB_MEMBER, MASTER_PLAIN_DB_MEMBER)

# ----- Extraction resource limits -----------------------------------------------
# These bounds are deliberately high enough for a large Rekordbox library while
# making archive bombs and unbounded JSON parsing explicit failures.
MAX_ARCHIVE_BYTES: int = 5 * 1024**3
MAX_MEMBER_COUNT: int = 100_000
MAX_MEMBER_BYTES: int = 2 * 1024**3
MAX_TOTAL_MEMBER_BYTES: int = 4 * 1024**3
MAX_JSON_MEMBER_BYTES: int = 16 * 1024**2


def _discover_folder_path_roots(master_plain_db: Path) -> list[str]:
    """Return the distinct absolute FolderPath top-level roots in ``djmdContent``.

    A "root" here is the first two path segments (e.g. ``/Users/user``) so the
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


def _required_regular_file_size(path: Path) -> int:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"required member missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"required member must be a regular file: {path}")
    return metadata.st_size


def _vocal_cache_files(vocal_cache_dir: Path) -> list[Path]:
    """Return contract-valid vocal cache entries, rejecting anything else.

    A snapshot must not become a covert transport for audio bytes, tokens,
    or arbitrary files dropped into the cache directory.  The cache contract
    is flat ``{stable_id}.json`` files only.
    """
    files: list[Path] = []
    for entry in sorted(vocal_cache_dir.iterdir()):
        metadata = entry.lstat()
        if not stat.S_ISREG(metadata.st_mode) or entry.suffix != ".json":
            raise ValueError(
                f"vocal cache contains unsupported member {entry}; only regular .json files may be packed"
            )
        if metadata.st_size > MAX_JSON_MEMBER_BYTES:
            raise ValueError(
                f"JSON member byte limit exceeded for {entry}: "
                f"{metadata.st_size} > {MAX_JSON_MEMBER_BYTES}"
            )
        payload = entry.read_bytes()
        if len(payload) != metadata.st_size:
            raise ValueError(
                f"vocal cache member changed while being read: {entry}"
            )
        _validate_json_object(payload, str(entry))
        files.append(entry)
    return files


def _validate_json_object(payload: bytes, label: str) -> None:
    if len(payload) > MAX_JSON_MEMBER_BYTES:
        raise ValueError(
            f"JSON member byte limit exceeded for {label}: "
            f"{len(payload)} > {MAX_JSON_MEMBER_BYTES}"
        )
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON in snapshot member {label}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"snapshot JSON member must contain an object: {label}")


def _validate_resource_limits(member_sizes: list[tuple[str, int]]) -> None:
    if len(member_sizes) > MAX_MEMBER_COUNT:
        raise ValueError(
            f"snapshot member count limit exceeded: "
            f"{len(member_sizes)} > {MAX_MEMBER_COUNT}"
        )
    total_bytes = 0
    for name, size in member_sizes:
        if size < 0 or size > MAX_MEMBER_BYTES:
            raise ValueError(
                f"snapshot member byte limit exceeded for {name}: "
                f"{size} > {MAX_MEMBER_BYTES}"
            )
        total_bytes += size
    if total_bytes > MAX_TOTAL_MEMBER_BYTES:
        raise ValueError(
            f"snapshot total member byte limit exceeded: "
            f"{total_bytes} > {MAX_TOTAL_MEMBER_BYTES}"
        )


def pack(data_dir: Path, out: Path) -> dict[str, Any]:
    """Bundle state.db + master.plain.db + vocal-cache into ``out``.

    Raises ``FileNotFoundError`` if state.db or master.plain.db is missing
    under ``data_dir`` -- fail fast, no partial snapshot.
    """
    state_db = data_dir / "state" / "state.db"
    master_plain_db = data_dir / "master.plain.db"
    vocal_cache_dir = data_dir / "state" / "vocal-cache"

    state_db_size = _required_regular_file_size(state_db)
    master_plain_db_size = _required_regular_file_size(master_plain_db)

    roots = _discover_folder_path_roots(master_plain_db)
    path_map_bytes = _generated_path_map_json(roots)
    cache_files = (
        _vocal_cache_files(vocal_cache_dir) if vocal_cache_dir.is_dir() else []
    )
    pack_sizes = [
        (STATE_DB_MEMBER, state_db_size),
        (MASTER_PLAIN_DB_MEMBER, master_plain_db_size),
        (PATH_MAP_MEMBER, len(path_map_bytes)),
    ]
    if vocal_cache_dir.is_dir():
        pack_sizes.append((VOCAL_CACHE_MEMBER, 0))
        pack_sizes.extend(
            (f"{VOCAL_CACHE_MEMBER}/{path.name}", path.stat().st_size)
            for path in cache_files
        )
    _validate_resource_limits(pack_sizes)

    members: list[dict[str, Any]] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w") as tar:
        tar.add(state_db, arcname=STATE_DB_MEMBER)
        members.append({"member": STATE_DB_MEMBER, "bytes": state_db_size})

        tar.add(master_plain_db, arcname=MASTER_PLAIN_DB_MEMBER)
        members.append(
            {"member": MASTER_PLAIN_DB_MEMBER, "bytes": master_plain_db_size}
        )

        if vocal_cache_dir.is_dir():
            tar.add(vocal_cache_dir, arcname=VOCAL_CACHE_MEMBER, recursive=False)
            for cache_file in cache_files:
                tar.add(cache_file, arcname=f"{VOCAL_CACHE_MEMBER}/{cache_file.name}")
            cache_bytes = sum(cache_file.stat().st_size for cache_file in cache_files)
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


def _classify_snapshot_member(
    member: tarfile.TarInfo,
    seen: set[str],
    required: dict[str, tarfile.TarInfo],
) -> None:
    name = member.name
    if "\\" in name:
        raise ValueError(f"snapshot member uses a Windows backslash alias: {name}")
    if name in seen:
        raise ValueError(f"snapshot contains duplicate member: {name}")
    seen.add(name)
    if name in REQUIRED_MEMBERS:
        if not member.isfile():
            raise ValueError(f"required snapshot member must be a regular file: {name}")
        required[name] = member
    elif name == PATH_MAP_MEMBER:
        if not member.isfile():
            raise ValueError(f"path map must be a regular file: {name}")
    elif name == VOCAL_CACHE_MEMBER:
        if not member.isdir():
            raise ValueError(f"vocal cache root must be a directory: {name}")
    elif name.startswith(f"{VOCAL_CACHE_MEMBER}/"):
        filename = name.removeprefix(f"{VOCAL_CACHE_MEMBER}/")
        if "/" in filename or not filename.endswith(".json"):
            raise ValueError(f"unsupported vocal cache member: {name}")
        if not member.isfile():
            raise ValueError(f"vocal cache member must be a regular file: {name}")
    else:
        raise ValueError(f"unsupported snapshot member: {name}")


def _validated_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Return the exact snapshot schema, rejecting traversal and payloads.

    This replaces ``extractall(filter='data')``, which is unavailable on the
    project's supported Python 3.11.  Whitelisting the small schema also
    makes secret or audio injection impossible even when a supplied tar has
    harmless-looking member names.
    """
    members = tar.getmembers()
    _validate_resource_limits([(member.name, member.size) for member in members])
    seen: set[str] = set()
    required: dict[str, tarfile.TarInfo] = {}
    for member in members:
        _classify_snapshot_member(member, seen, required)

    missing = [name for name in REQUIRED_MEMBERS if name not in required]
    if missing:
        raise ValueError(f"snapshot is missing required members: {missing}")
    return members


def _validate_json_members(
    tar: tarfile.TarFile, members: list[tarfile.TarInfo]
) -> None:
    for member in members:
        is_cache_json = member.name.startswith(f"{VOCAL_CACHE_MEMBER}/")
        if member.name != PATH_MAP_MEMBER and not is_cache_json:
            continue
        source = tar.extractfile(member)
        if source is None:
            raise ValueError(f"cannot read snapshot JSON member: {member.name}")
        with source:
            payload = source.read(MAX_JSON_MEMBER_BYTES + 1)
        _validate_json_object(payload, member.name)


def _extract_regular_file(member: tarfile.TarInfo, source: BinaryIO, dest: Path) -> None:
    """Write one validated member below ``dest`` without following escape paths."""
    root = dest.resolve()
    target = (dest / member.name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"snapshot member escapes destination: {member.name}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("wb") as out:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > member.size or written > MAX_MEMBER_BYTES:
                raise ValueError(
                    f"snapshot member exceeded declared size while extracting: "
                    f"{member.name}"
                )
            out.write(chunk)
    if written != member.size:
        target.unlink(missing_ok=True)
        raise ValueError(
            f"snapshot member size mismatch for {member.name}: "
            f"declared {member.size}, read {written}"
        )


def unpack(snapshot: Path, dest: Path) -> dict[str, Any]:
    """Extract ``snapshot`` into ``dest``.

    Raises ``FileNotFoundError`` if the tar itself is missing, and
    ``ValueError`` if any :data:`REQUIRED_MEMBERS` entry is absent from the
    tar -- fail fast rather than leaving a silently incomplete data dir.
    """
    if not snapshot.is_file():
        raise FileNotFoundError(f"snapshot not found: {snapshot}")
    archive_bytes = snapshot.stat().st_size
    if archive_bytes > MAX_ARCHIVE_BYTES:
        raise ValueError(
            f"snapshot archive byte limit exceeded: "
            f"{archive_bytes} > {MAX_ARCHIVE_BYTES}"
        )

    with tarfile.open(snapshot, "r") as tar:
        members = _validated_members(tar)
        _validate_json_members(tar, members)
        dest.mkdir(parents=True, exist_ok=True)
        for member in members:
            if member.isdir():
                target = (dest / member.name).resolve()
                try:
                    target.relative_to(dest.resolve())
                except ValueError as exc:
                    raise ValueError(f"snapshot member escapes destination: {member.name}") from exc
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = tar.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read snapshot member: {member.name}")
            with source:
                _extract_regular_file(member, source, dest)

    return {"dest": str(dest), "members": sorted(member.name for member in members)}


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
