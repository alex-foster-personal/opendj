"""``opendj library export --format rekordbox-xml --out FILE`` (LIBORG-10051).

Writes the same bytes as ``GET /api/v1/library/export.xml``: the whole live collection
and playlist folder tree as one rekordbox XML document. Reads the state DB directly, so
no running engine is needed. Writes ``<out>.partial`` and renames it onto ``<out>``, so
a failed export never leaves a truncated file at the target.

Requirements:
- ✔︎ ✅ 🎯 [if] the same library is exported by route and CLI [then ⛔️] the bytes differ
- ✔︎ ✅ 🎯 [if] ``--format m3u`` is given [then ⛔️] exit 1 and no file is written

``opendj library optimize --data-dir DIR --yes`` (LIBM-126, issue #12332) runs the same
confirmed SQLite optimize as ``POST /api/v1/library/maintenance/optimize`` on the named
data directory only; without ``--yes`` it prints the warning and exits 1 without running.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from apps.adapters.rekordbox_xml.library_export import ExportRefused, plan_library_export
from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, library_protection_cli
from apps.opendj_cli.refusal import fail as _fail
from apps.opendj_cli.track_cli import _resolve_state_db
from apps.shared.state import db_optimize
from apps.shared.state.db import open_ro
from apps.webui.server.analysis_overlay import lane_owned_fields

FORMATS = ("rekordbox-xml",)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise ValueError(message)


def _parser() -> _Parser:
    parser = _Parser(prog="opendj library", add_help=False)
    sub = parser.add_subparsers(dest="verb", required=True)
    export = sub.add_parser("export", add_help=False)
    export.add_argument("--format", choices=FORMATS, required=True)
    export.add_argument("--out", type=Path, required=True)
    optimize = sub.add_parser("optimize", add_help=False)
    optimize.add_argument("--data-dir", type=Path, required=True)
    optimize.add_argument("--yes", action="store_true")
    return parser


def _run_optimize(data_dir: Path, *, confirmed: bool, as_json: bool) -> int:
    """Optimize the named data directory's library DB only when ``--yes`` was given."""
    if not confirmed:
        return _fail(as_json, "confirm_required", f"pass --yes to optimize. {db_optimize.WARNING}", EXIT_FAILED)
    try:
        result = db_optimize.optimize_library_db(data_dir)
    except db_optimize.OptimizeRefused as exc:
        return _fail(as_json, exc.code.lower(), exc.message, EXIT_FAILED)
    except sqlite3.Error as exc:
        return _fail(as_json, "optimize_failed", f"optimize failed: {exc}", EXIT_FAILED)
    summary = {
        "status": "completed",
        "db_path": str(result.db_path),
        "bytes_before": result.bytes_before,
        "bytes_after": result.bytes_after,
        "elapsed_ms": result.elapsed_ms,
    }
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            f"optimized {result.db_path}: {result.bytes_before} -> {result.bytes_after} bytes in {result.elapsed_ms} ms"
        )
    return EXIT_CONFIRMED


def run(tokens: Sequence[str], *, as_json: bool, state_db: Path | None) -> int:
    if list(tokens)[:1] == ["protection"]:
        return library_protection_cli.run(list(tokens)[1:], as_json=as_json, state_db=state_db)
    try:
        args = _parser().parse_args(list(tokens))
    except ValueError as exc:
        return _fail(
            as_json,
            "usage",
            f"{exc}; usage: opendj library export --format rekordbox-xml --out FILE"
            " | opendj library optimize --data-dir DIR --yes",
            EXIT_FAILED,
        )
    if args.verb == "optimize":
        return _run_optimize(args.data_dir, confirmed=args.yes, as_json=as_json)
    db = _resolve_state_db(state_db)
    try:
        conn = open_ro(db)
    except FileNotFoundError as exc:
        return _fail(as_json, "state_db_missing", str(exc), EXIT_FAILED)
    try:
        export = plan_library_export(conn, lane_fields=functools.partial(lane_owned_fields, state_db_path=db))
    except ExportRefused as exc:
        return _fail(as_json, "export_refused", str(exc), EXIT_FAILED)
    out: Path = args.out
    partial = out.with_name(out.name + ".partial")
    try:
        with partial.open("wb") as fh:
            for chunk in export.iter_bytes():
                fh.write(chunk)
        os.replace(partial, out)
    except BaseException:
        export.close()
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        raise
    summary = {
        "out": str(out),
        "tracks": export.track_count,
        "playlists": export.playlist_count,
        "skipped_no_location": export.skipped_no_location,
        "skipped_streaming": export.skipped_streaming,
    }
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            f"wrote {export.track_count} tracks, {export.playlist_count} playlists to {out} "
            f"(skipped: {export.skipped_no_location} without a file, {export.skipped_streaming} streaming)"
        )
    return EXIT_CONFIRMED
