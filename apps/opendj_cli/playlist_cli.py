"""``opendj playlist thumbnail``, ``export``, ``summary``, ``import`` and ``save-order`` (ARTTHUMB-8728b, LIBORG-10314, DJIMPORT-10225, LIBUX-8678).

    opendj playlist thumbnail <playlist-id> [--size 64|232] [--out FILE]
    opendj playlist export <playlist-id> --format txt|csv|m3u8|rekordbox-xml [--out FILE]
    opendj playlist summary <playlist-id>
    opendj playlist import <file.m3u|file.m3u8> [--name NAME]
    opendj playlist save-order <playlist_id> (--by FIELD [--desc] | --ids ITEM_ID...)

``import`` posts the file to ``POST /api/v1/playlists/import``: a new playlist
of the tracks whose file paths match, in file order, with the unmatched paths
printed. A file with no match creates nothing and exits non-zero.

``export`` (LIBORG-10314) fetches ``GET /api/v1/playlists/{id}/export.txt``, the tab-separated
track list the tree's Export as text menu item downloads, and prints it or writes it to FILE.
``--format csv`` (LIBM-114) fetches ``GET /api/v1/playlists/{id}/export.csv`` the same way: the
UTF-8 (with BOM) CSV set list, written byte for byte. ``--format m3u8`` (LIBORG-9748) fetches
``GET /api/v1/playlists/{id}/export.m3u8``, the extended M3U8 playlist file; ``--json`` adds
``skipped``, the tracks left out for having no local file. ``--format rekordbox-xml``
(LIBORG-9751) fetches ``GET /api/v1/playlists/{id}/export.xml``, the playlist as rekordbox XML.

Same image as ``GET /api/v1/playlists/{id}/thumbnail``, the one the sidebar
rows and the playlists card grid show. Prints the kind (cover or
placeholder), the content type and the byte count; ``--out`` also writes the
image to FILE.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

REQUEST_TIMEOUT_S = 60.0
USAGE = (
    "usage: opendj playlist thumbnail <playlist-id> [--size 64|232] [--out FILE]\n"
    "       opendj playlist export <playlist-id> --format txt|csv|m3u8|rekordbox-xml [--out FILE]\n"
    "       opendj playlist summary <playlist-id>"
)
IMPORT_USAGE = "usage: opendj playlist import <file.m3u|file.m3u8> [--name NAME]"
IMPORT_PATH = "/api/v1/playlists/import"
PLAYLISTS_PATH = "/api/v1/playlists"
EXPORT_FORMATS = ("txt", "csv", "m3u8", "rekordbox-xml")
_EXPORT_SUFFIX = {"txt": "txt", "csv": "csv", "m3u8": "m3u8", "rekordbox-xml": "xml"}
SORT_FIELDS = ("bpm", "key", "title", "artist", "duration", "rating", "genre")
SAVE_ORDER_USAGE = "usage: opendj playlist save-order <playlist_id> (--by FIELD [--desc] | --ids ITEM_ID...)"


def thumbnail_path(playlist_id: str, size: int) -> str:
    return f"/api/v1/playlists/{quote(playlist_id, safe='')}/thumbnail?size={size}"


def export_path(playlist_id: str, export_format: str) -> str:
    return f"/api/v1/playlists/{quote(playlist_id, safe='')}/export.{_EXPORT_SUFFIX[export_format]}"


def summary_path(playlist_id: str) -> str:
    return f"/api/v1/playlists/{quote(playlist_id, safe='')}/summary"


class SaveOrderFailed(RuntimeError):
    """The engine refused the save; carries its status and body."""

    def __init__(self, status_code: int, body: Any) -> None:
        super().__init__(f"save-order returned {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def save_order_body(*, by: str | None, descending: bool, item_ids: list[str] | None) -> dict[str, Any]:
    """The request body; ValueError unless exactly one of ``by`` / ``item_ids`` is given."""
    if (by is None) == (item_ids is None):
        raise ValueError("give exactly one of --by or --ids")
    if by is not None:
        return {"by": by, "descending": descending}
    return {"item_ids": item_ids}


def save_order(client: httpx.Client, base_url: str, playlist_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Read the playlist's etag, then save the order under it."""
    url = f"{base_url}{PLAYLISTS_PATH}/{playlist_id}"
    current = client.get(url)
    if not current.is_success:
        raise SaveOrderFailed(current.status_code, current.text)
    response = client.post(f"{url}/items:save-order", json=body, headers={"If-Match": current.headers["ETag"]})
    if not response.is_success:
        raise SaveOrderFailed(response.status_code, response.text)
    return response.json()


def _save_order_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opendj playlist save-order")
    parser.add_argument("verb", choices=("save-order",))
    parser.add_argument("playlist_id")
    parser.add_argument("--by", choices=SORT_FIELDS)
    parser.add_argument("--desc", action="store_true", help="sort high to low")
    parser.add_argument("--ids", nargs="+", metavar="ITEM_ID")
    return parser


def run_save_order(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    try:
        args = _save_order_parser().parse_args(list(rest))
        body = save_order_body(by=args.by, descending=args.desc, item_ids=args.ids)
    except SystemExit:
        return _fail(as_json, "usage", SAVE_ORDER_USAGE, EXIT_FAILED)
    except ValueError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    try:
        base = resolve_backend_base_url(lock_path=lock).base_url.rstrip("/")
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            saved = save_order(client, base, args.playlist_id, body)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except (SaveOrderFailed, httpx.HTTPError) as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if as_json:
        print(json.dumps(saved, indent=2))
    else:
        print(f"saved the order of {saved['name']} ({saved['track_count']} tracks)")
    return EXIT_CONFIRMED


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
    if list(rest)[:1] == ["save-order"]:
        return run_save_order(rest, as_json=as_json, lock=lock)
    if list(rest)[:1] == ["import"]:
        return run_import(list(rest)[1:], as_json=as_json, lock=lock)
    if list(rest)[:1] == ["summary"]:
        if len(rest) != 2:
            return _fail(as_json, "usage", USAGE, EXIT_FAILED)
        return _run_summary(rest[1], as_json=as_json, lock=lock)
    parser = argparse.ArgumentParser(prog="opendj playlist", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=("thumbnail", "export"))
    parser.add_argument("playlist_id")
    parser.add_argument("--size", type=int, choices=(64, 232))
    parser.add_argument("--format", choices=EXPORT_FORMATS)
    parser.add_argument("--out", type=Path)
    try:
        args = parser.parse_args(list(rest))
    except SystemExit:
        return _fail(as_json, "usage", USAGE, EXIT_FAILED)
    if args.verb == "export":
        if args.format is None or args.size is not None:
            return _fail(as_json, "usage", USAGE, EXIT_FAILED)
        return _run_export(args.playlist_id, args.format, args.out, as_json=as_json, lock=lock)
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


def _run_summary(playlist_id: str, *, as_json: bool, lock: Path | None) -> int:
    response = _get(summary_path(playlist_id), as_json=as_json, lock=lock)
    if isinstance(response, int):
        return response
    payload = response.json()
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        bpm = payload["bpm"]
        energy = payload["energy"]
        print(
            f"{payload['tracks']} tracks, {payload['duration_s']:.0f}s, "
            f"bpm {bpm.get('min')}-{bpm.get('max')} ({bpm.get('median')}), "
            f"energy {energy.get('min')}-{energy.get('max')}, "
            f"{payload['keys']['distinct']} keys, {payload['unanalysed']} unanalysed"
        )
    return EXIT_CONFIRMED


def _export_rows(text: str, export_format: str) -> int:
    """Track rows in an export body, header excluded (CSV fields may hold quoted newlines)."""
    match export_format:
        case "csv":
            return len(list(csv.reader(io.StringIO(text.removeprefix("\ufeff"))))) - 1
        case "txt":
            return text.count("\n") - 1
        case "m3u8":
            return sum(1 for line in text.splitlines() if line and not line.startswith("#"))
        case "rekordbox-xml":
            return text.count('<TRACK Key="')
        case _:
            raise AssertionError(f"unhandled export format {export_format}")


def _run_export(playlist_id: str, export_format: str, out: Path | None, *, as_json: bool, lock: Path | None) -> int:
    response = _get(export_path(playlist_id, export_format), as_json=as_json, lock=lock)
    if isinstance(response, int):
        return response
    text = response.content.decode("utf-8")
    if out is not None:
        out.write_bytes(response.content)
    if as_json:
        rows = _export_rows(text, export_format)
        payload = {"playlist_id": playlist_id, "format": export_format, "rows": rows,
                   "out": str(out) if out is not None else None, "text": text}
        if "x-skipped" in response.headers:
            payload["skipped"] = int(response.headers["x-skipped"])
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
