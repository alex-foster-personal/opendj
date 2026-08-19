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
from typing import TextIO

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
    os.environ["MDT_DATA_DIR"] = str(data_dir)
    from apps.engine_core.setup.importer import SetupImportError, run_import

    progress_stream = sys.stdout
    emit = _emitter(progress_stream)
    emit(0.0, "starting the rekordbox import")

    with contextlib.redirect_stdout(sys.stderr):
        try:
            outcome = run_import(
                data_dir,
                emit=emit,
                source=Path(args.source) if args.source else None,
                limit=args.limit,
            )
        except SetupImportError as exc:
            # The code goes to stderr, which the runner keeps as the job's
            # error tail. Nothing partial is claimed on stdout.
            print(f"[ERROR] {exc.code}: {exc}", file=sys.stderr)
            return EXIT_FAILED

    emit(
        1.0,
        (
            f"imported {outcome.tracks} tracks and {outcome.playlists} "
            f"playlists; {outcome.analyses_linked} of "
            f"{outcome.analyses_expected} analyses resolve"
        ),
    )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
