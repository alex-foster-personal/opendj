"""The ``setup.import-rekordbox`` worker subprocess.

Contract with :mod:`apps.engine_core.jobs.runner`: stdout carries one JSON
object per line, ``{"progress": float, "message": str}``, and NOTHING else.
A single stray print would be read as an off-contract worker and get the
whole job killed, so stdout is redirected to stderr for the duration of the
run and the progress lines are written to a saved handle. The shared-state
CLI prints a full ingest summary; that summary is genuinely useful, so it
lands on stderr where the runner keeps it as the job's tail.

Ordering matters as much as it does in ``apps/engine_core/__main__.py``:
``MDT_DATA_DIR`` is set from ``--data-dir`` BEFORE the first
``apps.shared`` import, because those modules bind their paths at import
time and would otherwise silently address the wrong library.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:  # no runtime import: the env contract has to be set first
    from apps.engine_core.setup.importer import (
        FolderImportOutcome,
        ImportOutcome,
    )

EXIT_OK: int = 0
EXIT_FAILED: int = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.engine_core.setup.worker",
        description="import a rekordbox library into the engine's state db",
    )
    parser.add_argument(
        "--data-dir", required=True, help="absolute path to the data dir"
    )
    parser.add_argument(
        "--source",
        default=None,
        help="explicit rekordbox database to read (default: auto-detect)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="ingest at most N tracks (smoke-test aid)",
    )
    parser.add_argument(
        "--refresh-decrypt",
        action="store_true",
        help="re-decrypt the snapshot instead of reusing the plain copy",
    )
    parser.add_argument(
        "--mode",
        choices=("rekordbox", "folder"),
        default="rekordbox",
        help="import a rekordbox library, or walk plain folders of audio",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="folder to walk in --mode folder; repeat for several",
    )
    return parser


def _emitter(stream: TextIO):
    def emit(progress: float, message: str) -> None:
        stream.write(
            json.dumps({"progress": round(float(progress), 4), "message": message})
            + "\n"
        )
        stream.flush()

    return emit


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        print(
            f"[ERROR] --data-dir must be absolute, got {args.data_dir!r}",
            file=sys.stderr,
        )
        return EXIT_FAILED

    # Env contract first, imports second. See the module docstring.
    if args.mode == "folder" and not args.root:
        print("[ERROR] --mode folder needs at least one --root", file=sys.stderr)
        return EXIT_FAILED

    os.environ["MDT_DATA_DIR"] = str(data_dir)
    from apps.shared.process_identity import set_process_identity

    set_process_identity("worker", invocation_argv=sys.orig_argv)
    from apps.engine_core.setup.importer import (
        SetupImportError,
        run_folder_import,
        run_import,
    )

    progress_stream = sys.stdout
    emit = _emitter(progress_stream)
    emit(0.0, f"starting the {args.mode} import")

    with contextlib.redirect_stdout(sys.stderr):
        try:
            if args.mode == "folder":
                summary = _folder_summary(
                    run_folder_import(
                        data_dir,
                        emit=emit,
                        roots=[Path(root).expanduser() for root in args.root],
                        limit=args.limit,
                    )
                )
            else:
                summary = _rekordbox_summary(
                    run_import(
                        data_dir,
                        emit=emit,
                        source=Path(args.source) if args.source else None,
                        limit=args.limit,
                        refresh_decrypt=args.refresh_decrypt,
                    )
                )
        except SetupImportError as exc:
            # The code goes to stderr, which the runner keeps as the job's
            # error tail. Nothing partial is claimed on stdout.
            print(f"[ERROR] {exc.code}: {exc}", file=sys.stderr)
            return EXIT_FAILED

    emit(1.0, summary)
    return EXIT_OK


def _rekordbox_summary(outcome: ImportOutcome) -> str:
    return (
        f"imported {outcome.tracks} tracks and {outcome.playlists} "
        f"playlists; {outcome.analyses_linked} of "
        f"{outcome.analyses_expected} analyses resolve"
    )


def _folder_summary(outcome: FolderImportOutcome) -> str:
    """Says what was NOT done as loudly as what was."""
    denied = outcome.unreadable_roots
    caveat = (
        f"; {len(denied)} folder(s) could not be read ({', '.join(denied)})"
        if denied
        else ""
    )
    rejected = (
        f"; {outcome.files_rejected_unplayable} file(s) skipped as unplayable"
        if outcome.files_rejected_unplayable
        else ""
    )
    return (
        f"imported {outcome.tracks_written} of {outcome.files_seen} "
        f"readable audio files, none of them analysed{rejected}{caveat}"
    )


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
