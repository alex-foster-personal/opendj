"""``opendj ingest``: HTTP parity for the ingest drop modal staging workflow.

    opendj ingest stage PATH [PATH ...] [--batch B] [--force] [--no-refresh] [--dry-run]

Multipart upload to ``POST /api/v1/ingest/upload``, then the same conditional
``POST /api/v1/ingest/refresh`` the UI runs after ``uploadIngestFiles``.
Follow-up refresh inspection: ``opendj api GET /api/v1/ingest/refresh/status``.
``--dry-run`` prints the batch and the files that would be uploaded without
contacting the engine.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, exit_for_status, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail
from apps.shared.paths import AUDIO_EXTENSIONS

UPLOAD_PATH = "/api/v1/ingest/upload"
REFRESH_PATH = "/api/v1/ingest/refresh"
UPLOAD_TIMEOUT_S = 120.0
REFRESH_TIMEOUT_S = 30.0

AUDIO_EXT_RE = re.compile(
    r"\.(mp3|m4a|aac|wav|aiff?|flac|ogg|alac)$",
    re.IGNORECASE,
)


def _utc_stamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


def default_batch_name() -> str:
    suffix = f"{_utc_stamp(datetime.now(UTC))}-{secrets.token_hex(4)}"
    return f"drop-{suffix}"


def _is_audio_path(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS or bool(AUDIO_EXT_RE.search(path.name))


def collect_upload_paths(paths: Sequence[Path]) -> list[tuple[str, Path]]:
    """Return ``(multipart filename, absolute path)`` pairs for one stage request."""
    collected: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for raw in paths:
        path = raw.expanduser().resolve()
        if not path.exists():
            raise ValueError(f"path does not exist: {raw}")
        if path.is_file():
            if not _is_audio_path(path):
                continue
            key = ("", path.name)
            if key not in seen:
                seen.add(key)
                collected.append((path.name, path))
            continue
        if not path.is_dir():
            raise ValueError(f"not a file or directory: {raw}")
        for child in sorted(path.rglob("*")):
            if not child.is_file() or not _is_audio_path(child):
                continue
            rel = child.relative_to(path).as_posix()
            key = (str(path), rel)
            if key in seen:
                continue
            seen.add(key)
            collected.append((rel, child))
    if not collected:
        raise ValueError("no audio files found in the given path(s)")
    return collected


def should_start_refresh(upload: dict[str, Any], *, no_refresh: bool) -> bool:
    """Mirror ``IngestDropModal._maybeStartRefresh`` (without pending poll)."""
    if no_refresh:
        return False
    results = upload.get("results")
    if not isinstance(results, list):
        return False
    if any(isinstance(row, dict) and row.get("verdict") == "possible_duplicate" for row in results):
        return False
    return sum(1 for row in results if isinstance(row, dict) and row.get("verdict") == "new") > 0


def _emit_response_error(response: httpx.Response) -> None:
    if response.content:
        sys.stderr.buffer.write(response.content)
        if not response.content.endswith(b"\n"):
            sys.stderr.buffer.write(b"\n")


def _request_multipart_upload(
    *,
    base_url: str,
    batch: str,
    force: bool,
    pairs: Sequence[tuple[str, Path]],
) -> httpx.Response:
    url = f"{base_url.rstrip('/')}{UPLOAD_PATH}"
    with httpx.Client(timeout=UPLOAD_TIMEOUT_S) as client, ExitStack() as stack:
        files: list[tuple[str, tuple[str, object, str]]] = []
        for rel_name, abs_path in pairs:
            handle = stack.enter_context(abs_path.open("rb"))
            files.append(("files", (rel_name, handle, "application/octet-stream")))
        return client.post(
            url,
            data={"batch": batch, "force": str(force).lower()},
            files=files,
        )


def _request_refresh(*, base_url: str, batch_dir: str) -> httpx.Response:
    url = f"{base_url.rstrip('/')}{REFRESH_PATH}"
    with httpx.Client(timeout=REFRESH_TIMEOUT_S) as client:
        return client.post(url, json={"batch_dir": batch_dir})


def _text(upload: dict[str, Any], refresh: dict[str, Any] | None) -> str:
    lines = [f"batch: {upload.get('batch')}", f"dest_dir: {upload.get('dest_dir')}"]
    results = upload.get("results")
    if isinstance(results, list):
        for row in results:
            if not isinstance(row, dict):
                continue
            lines.append(f"  {row.get('filename')}: verdict={row.get('verdict')} staged={row.get('staged_path')}")
    if refresh is not None:
        lines.append(f"refresh.running: {refresh.get('running')}")
        lines.append(f"refresh.phase: {refresh.get('phase')}")
        if refresh.get("started_at"):
            lines.append(f"refresh.started_at: {refresh.get('started_at')}")
    return "\n".join(lines)


def _stage_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opendj ingest", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    stage = sub.add_parser("stage", help="upload audio paths and maybe start batch refresh")
    stage.add_argument("paths", nargs="+", type=Path)
    stage.add_argument("--batch")
    stage.add_argument("--force", action="store_true")
    stage.add_argument("--no-refresh", action="store_true")
    stage.add_argument("--dry-run", action="store_true", help="list what would be staged; send nothing")
    return parser


def _parse_stage_args(
    rest: Sequence[str],
    *,
    as_json: bool,
) -> tuple[argparse.Namespace | None, int | None]:
    try:
        args = _stage_parser().parse_args(list(rest))
    except SystemExit:
        return None, _fail(
            as_json,
            "usage",
            "usage: opendj ingest stage PATH [PATH ...] [--batch B] [--force] [--no-refresh] [--dry-run]",
            EXIT_FAILED,
        )
    if args.verb != "stage":
        return None, _fail(as_json, "usage", f"unknown ingest verb {args.verb!r}", EXIT_FAILED)
    return args, None


def _perform_upload(
    *,
    base_url: str,
    batch: str,
    force: bool,
    pairs: Sequence[tuple[str, Path]],
    as_json: bool,
) -> tuple[dict[str, Any] | None, int | None]:
    try:
        upload_response = _request_multipart_upload(
            base_url=base_url,
            batch=batch,
            force=force,
            pairs=pairs,
        )
    except httpx.HTTPError as error:
        return None, _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if not upload_response.is_success:
        _emit_response_error(upload_response)
        return None, exit_for_status(upload_response.status_code)
    return upload_response.json(), None


def _perform_refresh(
    upload: dict[str, Any],
    *,
    base_url: str,
    no_refresh: bool,
    as_json: bool,
) -> tuple[dict[str, Any] | None, int | None]:
    if not should_start_refresh(upload, no_refresh=no_refresh):
        return None, None
    dest_dir = upload.get("dest_dir")
    if not isinstance(dest_dir, str) or dest_dir == "":
        return None, _fail(as_json, "request_failed", "upload response missing dest_dir", EXIT_FAILED)
    try:
        refresh_response = _request_refresh(base_url=base_url, batch_dir=dest_dir)
    except httpx.HTTPError as error:
        return None, _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if not refresh_response.is_success:
        _emit_response_error(refresh_response)
        return None, exit_for_status(refresh_response.status_code)
    payload = refresh_response.json()
    refresh = payload if isinstance(payload, dict) else None
    return refresh, None


def _print_dry_run(batch: str, pairs: Sequence[tuple[str, Path]], *, as_json: bool) -> None:
    """Print the batch and files a stage run would upload, without sending anything."""
    if as_json:
        files = [{"filename": name, "path": str(path)} for name, path in pairs]
        print(json.dumps({"dry_run": True, "batch": batch, "files": files}, indent=2, sort_keys=True))
        return
    lines = [f"dry run: would stage {len(pairs)} file(s) into batch {batch}"]
    lines.extend(f"  {name}: {path}" for name, path in pairs)
    print("\n".join(lines))


def _print_stage_result(
    upload: dict[str, Any],
    refresh: dict[str, Any] | None,
    *,
    as_json: bool,
) -> None:
    if as_json:
        document: dict[str, Any] = {"upload": upload}
        if refresh is not None:
            document["refresh"] = refresh
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(_text(upload, refresh))


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    args, parse_code = _parse_stage_args(rest, as_json=as_json)
    if parse_code is not None:
        return parse_code

    batch = (args.batch or default_batch_name()).strip()

    try:
        pairs = collect_upload_paths(args.paths)
    except ValueError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)

    if args.dry_run:
        _print_dry_run(batch, pairs, as_json=as_json)
        return EXIT_CONFIRMED

    try:
        target = resolve_backend_base_url(lock_path=lock)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)

    upload, upload_code = _perform_upload(
        base_url=target.base_url,
        batch=batch,
        force=args.force,
        pairs=pairs,
        as_json=as_json,
    )
    if upload_code is not None:
        return upload_code

    refresh, refresh_code = _perform_refresh(
        upload,
        base_url=target.base_url,
        no_refresh=args.no_refresh,
        as_json=as_json,
    )
    if refresh_code is not None:
        return refresh_code

    _print_stage_result(upload, refresh, as_json=as_json)
    return EXIT_CONFIRMED
