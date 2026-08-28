"""CLI driver: ``python -m apps.analysis.run``.

Iterates a queue of ``(stable_id, path)`` tuples, runs the selected backend,
and persists the result through :mod:`apps.analysis.store`.  Idempotent on
``(stable_id, backend, backend_version)`` so re-running is free.

Flags
-----

``--dry-run``            Do not write to the state DB; print a summary only.
``--limit N``            Cap the queue at ``N`` tracks.
``--backend NAME``       Backend to use (default: ``librosa+madmom``).
``--workers N``          Process-pool worker count (default: 1).
``--files PATH [..]``    Explicit audio file list; stable_ids are DERIVED
                         (``pathid_*``/``bytesid_*``) per the strategy flag.
``--pairs-json PATH``    JSON file ``[[stable_id, path], ...]`` carrying the
                         CANONICAL state-layer stable_ids, so analysis rows
                         land under the same key coverage reads. Mutually
                         exclusive with ``--files``.
``--all``                Disable the only-missing filter (default is on).
``--stable-id-strategy`` ``file-path`` (default) or ``sha256``.
``--verbose``            Log per-track progress.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.table import Table

from .backends import get_backend
from .backends.base import BackendNotAvailable, TrackTooLong
from .record import AnalysisRecord
from .store import fetch_records_by_ids, open_conn, upsert_record

log = logging.getLogger("apps.analysis.run")
console = Console()


# ---------------------------------------------------------------------------
# Track queue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackRef:
    """A track to analyse.  ``stable_id`` is authoritative."""

    stable_id: str
    path: Path


def stable_id_from_path(path: Path) -> str:
    """Deterministic placeholder stable_id until Phase 5's ingest runs."""
    abspath = str(path.resolve())
    return "pathid_" + hashlib.sha256(abspath.encode("utf-8")).hexdigest()[:16]


def stable_id_from_audio_bytes(path: Path) -> str:
    """Content-hashed stable_id; robust to path renames."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "bytesid_" + h.hexdigest()[:32]


def build_queue(
    files: Iterable[Path],
    *,
    strategy: str = "file-path",
    limit: int | None = None,
) -> list[TrackRef]:
    refs: list[TrackRef] = []
    for p in files:
        if not p.exists():
            log.warning("skip (missing): %s", p)
            continue
        if strategy == "sha256":
            sid = stable_id_from_audio_bytes(p)
        else:
            sid = stable_id_from_path(p)
        refs.append(TrackRef(stable_id=sid, path=p))
        if limit and len(refs) >= limit:
            break
    return refs


def build_queue_from_pairs(pairs: Iterable[tuple[str, str]]) -> list[TrackRef]:
    """Queue from caller-supplied canonical ``(stable_id, path)`` pairs.

    No identity derivation: the caller (e.g. the webui refresh job) already
    knows the state-layer stable_id, and deriving a ``pathid_*`` here would
    store the analysis row under a key coverage never matches.
    """
    refs: list[TrackRef] = []
    for sid, path_str in pairs:
        if not sid:
            raise ValueError(f"empty stable_id for {path_str!r} in pairs input")
        path = Path(path_str)
        if not path.exists():
            log.warning("skip (missing): %s", path)
            continue
        refs.append(TrackRef(stable_id=sid, path=path))
    return refs


def filter_missing(
    refs: list[TrackRef],
    *,
    backend_name: str,
    db_path: Path | None = None,
) -> list[TrackRef]:
    """Drop tracks that already have an analysis row for ``backend_name``."""
    if not refs:
        return refs
    existing = fetch_records_by_ids(
        [r.stable_id for r in refs], backend=backend_name, db_path=db_path
    )
    seen = {r.stable_id for r in existing}
    return [r for r in refs if r.stable_id not in seen]


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _analyze_one(
    backend_name: str, stable_id: str, path_str: str
) -> tuple[str, AnalysisRecord | None, str | None]:
    try:
        backend = get_backend(backend_name)
        rec = backend.analyze(Path(path_str), stable_id)
        return stable_id, rec, None
    except (BackendNotAvailable, TrackTooLong) as exc:
        return stable_id, None, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover
        return stable_id, None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    analysed: int = 0
    skipped_existing: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def run(
    queue: list[TrackRef],
    *,
    backend_name: str = "librosa+madmom",
    dry_run: bool = False,
    limit: int | None = None,
    workers: int = 1,
    only_missing: bool = True,
    verbose: bool = False,
    db_path: Path | None = None,
) -> RunSummary:
    """Run the analysis pipeline over a prebuilt queue.

    Callers own identity: build the queue with :func:`build_queue` (derived
    pathid_*/bytesid_* placeholders) or :func:`build_queue_from_pairs`
    (canonical state-layer stable_ids).
    """
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if only_missing and not dry_run:
        queue = filter_missing(queue, backend_name=backend_name, db_path=db_path)

    pre_limit_count = len(queue)
    if limit is not None:
        queue = queue[:limit]

    summary = RunSummary()

    if not queue:
        console.print("[yellow]Empty queue; nothing to do.[/yellow]")
        return summary

    console.print(
        f"[cyan]Analysing {len(queue)} tracks with backend={backend_name} "
        f"(pool={workers}, dry_run={dry_run}, pre-limit={pre_limit_count})[/cyan]"
    )

    rows: list[tuple[str, AnalysisRecord | None, str | None]] = []

    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(_analyze_one, backend_name, r.stable_id, str(r.path))
                for r in queue
            ]
            for fut in as_completed(futs):
                rows.append(fut.result())
    else:
        for r in queue:
            rows.append(_analyze_one(backend_name, r.stable_id, str(r.path)))

    table = Table(title="analysis run")
    table.add_column("stable_id")
    table.add_column("bpm")
    table.add_column("key")
    table.add_column("energy")
    table.add_column("duration_s")
    table.add_column("status")

    # Share one state-DB connection across the whole batch when writing.
    # upsert()/publish() previously opened + closed per call, which forced
    # thousands of WAL checkpoints on multi-track runs; a single shared
    # connection keeps the batch in one WAL window. Only opened for live
    # writes; dry-run stays pure.
    shared_conn = open_conn(db_path) if not dry_run else None
    try:
        for sid, rec, err in rows:
            if rec is None:
                summary.failed += 1
                summary.errors.append((sid, err or "unknown error"))
                table.add_row(sid[:14], "-", "-", "-", "-", f"[red]{err}[/red]")
                continue
            status = "dry-run" if dry_run else "-"
            if not dry_run:
                result = upsert_record(rec, db_path=db_path, conn=shared_conn)
                status = (
                    "new" if result.inserted
                    else "unchanged" if result.unchanged
                    else "updated"
                )
                if result.unchanged:
                    summary.skipped_existing += 1
                else:
                    summary.analysed += 1
            else:
                summary.analysed += 1
            table.add_row(
                sid[:14],
                f"{rec.bpm:.1f}",
                rec.key_camelot,
                str(rec.energy),
                f"{rec.duration_s:.1f}",
                status,
            )
    finally:
        if shared_conn is not None:
            shared_conn.close()

    console.print(table)
    console.print(
        f"[green]ok[/green] analysed={summary.analysed} "
        f"skipped={summary.skipped_existing} failed={summary.failed}"
    )
    if summary.errors:
        for sid, err in summary.errors[:10]:
            console.print(f"  [red]ERR {sid[:14]}[/red] {err}")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.analysis.run",
        description="Run the analyser pipeline over a set of audio files.",
    )
    parser.add_argument("--backend", default="librosa+madmom")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--only-missing", action="store_true", default=True,
        help="skip tracks that already have a row (default on)",
    )
    parser.add_argument(
        "--all", dest="only_missing", action="store_false",
        help="disable only-missing; re-analyse every track",
    )
    parser.add_argument(
        "--stable-id-strategy",
        choices=["file-path", "sha256"],
        default="file-path",
    )
    parser.add_argument("--verbose", action="store_true")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--files", nargs="+",
        help=(
            "Audio file paths; stable_ids derived per --stable-id-strategy "
            "(pathid_*/bytesid_* placeholders)."
        ),
    )
    source.add_argument(
        "--pairs-json", type=Path,
        help=(
            "JSON file [[stable_id, path], ...] carrying canonical "
            "state-layer stable_ids (no derivation)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.pairs_json is not None:
        raw = json.loads(args.pairs_json.read_text())
        if not isinstance(raw, list) or not all(
            isinstance(e, list) and len(e) == 2 for e in raw
        ):
            raise SystemExit(
                f"--pairs-json {args.pairs_json}: expected [[stable_id, path], ...]"
            )
        queue = build_queue_from_pairs([(sid, path) for sid, path in raw])
    else:
        queue = build_queue(
            [Path(p) for p in args.files], strategy=args.stable_id_strategy
        )
    summary = run(
        queue,
        backend_name=args.backend,
        dry_run=args.dry_run,
        limit=args.limit,
        workers=args.workers,
        only_missing=args.only_missing,
        verbose=args.verbose,
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
