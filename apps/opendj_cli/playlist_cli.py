"""``opendj playlist thumbnail``, ``export`` and ``import`` (ARTTHUMB-8728b, LIBORG-10314, DJIMPORT-10225).

    opendj playlist thumbnail <playlist-id> [--size 64|232] [--out FILE]
    opendj playlist export <playlist-id> --format txt [--out FILE]
    opendj playlist import <file.m3u|file.m3u8> [--name NAME]

``import`` posts the file to ``POST /api/v1/playlists/import``: a new playlist
of the tracks whose file paths match, in file order, with the unmatched paths
printed. A file with no match creates nothing and exits non-zero.

``export`` (LIBORG-10314) fetches ``GET /api/v1/playlists/{id}/export.txt``, the tab-separated
track list the tree's Export as text menu item downloads, and prints it or writes it to FILE.

Same image as ``GET /api/v1/playlists/{id}/thumbnail``, the one the sidebar
rows and the playlists card grid show. Prints the kind (cover or
placeholder), the content type and the byte count; ``--out`` also writes the
image to FILE.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

REQUEST_TIMEOUT_S = 60.0
USAGE = (
    "usage: opendj playlist thumbnail <playlist-id> [--size 64|232] [--out FILE]\n"
    "       opendj playlist export <playlist-id> --format txt [--out FILE]"
)
IMPORT_USAGE = "usage: opendj playlist import <file.m3u|file.m3u8> [--name NAME]"
IMPORT_PATH = "/api/v1/playlists/import"


def thumbnail_path(playlist_id: str, size: int) -> str:
    return f"/api/v1/playlists/{quote(playlist_id, safe='')}/thumbnail?size={size}"


def export_path(playlist_id: str) -> str:
    return f"/api/v1/playlists/{quote(playlist_id, safe='')}/export.txt"


def run_import(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    """``opendj playlist import <file>``: the same import ``POST /api/v1/playlists/import`` does."""
    parser = argparse.ArgumentParser(prog="opendj playlist import")
    parser.add_argument("file", type=Path)
    parser.add_argument("--name")
    try:
        args = parser.parse_args(list(rest))
    except SystemExit:
        return _fail(as_json, "usage", IMPORT_USAGE, EXIT_FAILED)
    source = args.file.resolve()
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as error:
        return _fail(as_json, "unreadable_file", f"cannot read {source}: {error}", EXIT_FAILED)
    body = {"name": args.name or source.stem, "m3u": text, "base_dir": str(source.parent)}
    try:
        target = resolve_backend_base_url(lock_path=lock)
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            response = client.post(f"{target.base_url.rstrip('/')}{IMPORT_PATH}", json=body)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if not response.is_success:
        return _fail(as_json, "request_failed", f"POST {IMPORT_PATH} returned {response.status_code}: {response.text}",
                     EXIT_FAILED)
    payload = response.json()
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{payload['name']} ({payload['playlist_id']}): {payload['matched']} matched, "
              f"{len(payload['unmatched'])} unmatched")
        for path in payload["unmatched"]:
            print(f"  unmatched: {path}")
    return EXIT_CONFIRMED


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    if list(rest)[:1] == ["import"]:
        return run_import(list(rest)[1:], as_json=as_json, lock=lock)
    parser = argparse.ArgumentParser(prog="opendj playlist", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=("thumbnail", "export"))
    parser.add_argument("playlist_id")
    parser.add_argument("--size", type=int, choices=(64, 232))
    parser.add_argument("--format", choices=("txt",))
    parser.add_argument("--out", type=Path)
    try:
        args = parser.parse_args(list(rest))
    except SystemExit:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    if args.verb == "export":
        if args.format is None or args.size is not None:
            return _fail(as_json, "usage", USAGE, EXIT_FAILED)
        return _run_export(args.playlist_id, args.out, as_json=as_json, lock=lock)
    if args.verb == "thumbnail":
        if args.format is not None:
            return _fail(as_json, "usage", USAGE, EXIT_FAILED)
        return _run_thumbnail(args.playlist_id, 64 if args.size is None else args.size, args.out, as_json=as_json,
                              lock=lock)
    raise AssertionError(f"unhandled playlist verb {args.verb}")


def _get(path: str, *, as_json: bool, lock: Path | None) -> httpx.Response | int:
    """GET ``path`` on the running engine, or the CLI exit code of the failure."""
    try:
        target = resolve_backend_base_url(lock_path=lock)
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            response = client.get(f"{target.base_url.rstrip('/')}{path}")
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if not response.is_success:
        return _fail(as_json, "request_failed", f"GET {path} returned {response.status_code}: {response.text}",
                     EXIT_FAILED)
    return response


def _run_export(playlist_id: str, out: Path | None, *, as_json: bool, lock: Path | None) -> int:
    response = _get(export_path(playlist_id), as_json=as_json, lock=lock)
    if isinstance(response, int):
        return response
    text = response.content.decode("utf-8")
    if out is not None:
        out.write_bytes(response.content)
    if as_json:
        rows = text.count("\n") - 1
        payload = {"playlist_id": playlist_id, "format": "txt", "rows": rows,
                   "out": str(out) if out is not None else None, "text": text}
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif out is None:
        print(text, end="")
    else:
        print(f"wrote {len(response.content)} bytes -> {out}")
    return EXIT_CONFIRMED


def _run_thumbnail(playlist_id: str, size: int, out: Path | None, *, as_json: bool, lock: Path | None) -> int:
    response = _get(thumbnail_path(playlist_id, size), as_json=as_json, lock=lock)
    if isinstance(response, int):
        return response
    if out is not None:
        out.write_bytes(response.content)
    payload = {
        "playlist_id": playlist_id,
        "size": size,
        "kind": response.headers["x-thumbnail-kind"],
        "content_type": response.headers["content-type"],
        "bytes": len(response.content),
        "out": str(out) if out is not None else None,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{payload['kind']} {payload['content_type']} {payload['bytes']} bytes"
              + (f" -> {payload['out']}" if payload["out"] else ""))
    return EXIT_CONFIRMED
