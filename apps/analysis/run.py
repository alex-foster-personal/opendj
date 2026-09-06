"""CLI driver: ``python -m apps.analysis.run``.

Iterates a queue of ``(stable_id, path)`` tuples, runs the selected backend,
and persists the result through :mod:`apps.analysis.store`.  Idempotent on
``(stable_id, backend, backend_version)`` so re-running is free.

Flags
-----

``--dry-run``            Do not write to the state DB; print a summary only.
``--limit N``            Cap the queue at ``N`` tracks.
``--backend NAME``       Backend to use (default: ``librosa``).
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

Exit codes
----------

``0``  Every queued track was attempted and none failed.
``1``  ``EXIT_TRACK_FAILURES`` -- at least one track was attempted and its
       backend failed on it. The only status that says nothing about the
       next chunk, so the only one a chunking caller may continue past.
``2``  ``EXIT_USAGE`` -- bad flags, an unknown backend, a malformed handoff
       file. Argparse's own convention, kept distinct from ``1`` so a
       configuration mistake is not read as "some tracks failed".
``3``  ``EXIT_MISSING_TARGETS`` -- one or more ``--pairs-json`` targets were
       gone before they could be analyzed, either at the admission check or
       later, when the backend opened the file. Never attempted either way.
       Distinct from ``1`` on purpose: the caller must retry that queue
       rather than record it as tried.
``4``  ``EXIT_BACKEND_UNAVAILABLE`` -- nothing was analyzed and every failure
       was ``BackendNotAvailable``. A fact about this machine, not these
       files, so the caller must stop rather than work through the rest of
       the library rediscovering it one chunk at a time.
``5``  ``EXIT_INTERNAL_ERROR`` -- an exception escaped the run itself, so no
       per-track verdict was reached at all. Also systemic: the caller must
       stop. Exists because Python exits 1 for an escaping exception, which
       would otherwise read as "some tracks failed".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import multiprocessing
import sys
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .backends import DEFAULT_BACKEND, get_backend
from .backends.base import (
    AnalyzerBackend,
    BackendNotAvailable,
    TrackTooLong,
    TrackUnreadable,
    TrackVanished,
)
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


#: Exit code for "some tracks failed, on their own merits". The only status
#: a caller may keep going on: it says nothing about the next chunk.
EXIT_TRACK_FAILURES: int = 1

#: Exit code for a USAGE error: bad flags, an unknown backend, a malformed
#: handoff file. Argparse's own convention, and distinct from 1 on purpose -
#: a bare ``raise SystemExit("message")`` also exits 1, which would make a
#: configuration mistake indistinguishable from "some tracks failed" and
#: invite a chunking caller to retry it against the same bad configuration.
EXIT_USAGE: int = 2

#: Exit code for "the caller's targets were not all analyzed here". The pairs
#: handoff names files the caller verified moments earlier, so one missing by
#: the time this process reaches it means the drain never attempted it. That
#: is a different fact from an analyzer failing on a file that IS present
#: (exit 1): the caller must retry the queue rather than record it as tried.
#: Covers both moments a target can be lost - the admission check in
#: :func:`build_queue_from_pairs`, and the narrower race where the file
#: survives admission and is gone when the backend opens it
#: (:class:`~apps.analysis.backends.base.TrackVanished`).
EXIT_MISSING_TARGETS: int = 3

#: Exit code for "the BACKEND could not run, so nothing was analyzed". Every
#: track failed and every failure was BackendNotAvailable, which is a
#: capability fact about this machine, not a fact about these files. The
#: caller must stop rather than work through the rest of the library
#: discovering the same thing per chunk.
EXIT_BACKEND_UNAVAILABLE: int = 4

#: Exit code for "the run itself came apart", i.e. an exception escaped
#: :func:`run` and no track ever got a verdict: the state DB could not be
#: opened, the disk filled mid-write, the process pool died. Systemic, so the
#: caller must stop.
#:
#: It exists because CPython's exit status for an escaping exception is 1,
#: which is EXIT_TRACK_FAILURES - the one status a chunking caller is allowed
#: to continue past. Without a distinct code, a full disk is indistinguishable
#: from "some tracks failed" and the caller works through the whole library
#: meeting the same wall once per chunk. The traceback is logged first, so
#: nothing is swallowed: only the status is translated.
EXIT_INTERNAL_ERROR: int = 5


def vanished_count(summary: RunSummary) -> int:
    """How many targets were gone by the time the backend opened them.

    The admission check in :func:`build_queue_from_pairs` catches the common
    case, but it is a check against a filesystem that keeps moving: a sync,
    a rename or an unmount between that check and the decode leaves a target
    that this process accepted and never attempted. Counted apart from the
    rest of ``failed`` because the exit status turns on it - see
    :data:`EXIT_MISSING_TARGETS`.

    Keyed on the error text the same way :func:`backend_never_ran` is, but
    derived from the class name rather than spelled out, so renaming the
    exception cannot silently stop matching.
    """
    marker = f"{TrackVanished.__name__}:"
    return sum(1 for _, msg in summary.errors if msg.startswith(marker))


def backend_never_ran(summary: RunSummary) -> bool:
    """True when nothing was analyzed and every failure was the backend.

    Deliberately conjunctive. One successful track proves the backend works
    on this box, so any failures alongside it are about the files and the
    caller must keep going. ``skipped_existing`` counts as working too: the
    run reached the store.
    """
    return (
        summary.failed > 0
        and summary.analysed == 0
        and summary.skipped_existing == 0
        and all(
            msg.startswith("BackendNotAvailable:") for _, msg in summary.errors
        )
    )


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
    backend: str | type[AnalyzerBackend], stable_id: str, path_str: str
) -> tuple[str, AnalysisRecord | None, str | None]:
    """Analyze one track, or say why this FILE could not be.

    Only failures a backend has declared to be about the input are turned
    into a per-track error here. Everything else propagates: a broad catch at
    this level relabels a machine-wide fault - an unreadable analyzer config,
    a backend that failed to initialize, a dead worker - as "this file
    failed", which the CLI then reports as EXIT_TRACK_FAILURES, which is the
    one status a chunking caller is allowed to continue past. The caller then
    meets the identical fault once per chunk across the whole library.

    ``get_backend`` is outside the guard for the same reason: an unresolvable
    backend name is a configuration fact, not a property of this track.
    ``run`` resolves the name once and hands workers the CLASS: a ``spawn``
    worker imports it by reference instead of re-reading a registry that only
    the parent process has populated. A name is still accepted for callers
    that analyze a single track in-process.
    """
    if isinstance(backend, str):
        backend = get_backend(backend)
    try:
        rec = backend.analyze(Path(path_str), stable_id)
    except (
        BackendNotAvailable, TrackTooLong, TrackUnreadable, TrackVanished
    ) as exc:
        return stable_id, None, f"{type(exc).__name__}: {exc}"
    return stable_id, rec, None


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
    backend_name: str = DEFAULT_BACKEND,
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

    backend = get_backend(backend_name)
    if workers > 1:
        # ``spawn``, not the Linux default ``fork``: a worker forked after
        # librosa, numba and OpenBLAS have initialised in this process
        # inherits their thread and JIT state and can jump through a null
        # pointer on first use. agentbox logged 238 python3 ``segfault at 0
        # ip 0`` kernel entries on Sat 5 Sep 2026 and the e2e Root suite died
        # five times on BrokenProcessPool (#1316); pinning native threads to 1
        # in the job environment (#1157, #1315) did not stop it. A spawned
        # worker starts clean and imports what it needs itself.
        with ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            futs = [
                pool.submit(_analyze_one, backend, r.stable_id, str(r.path))
                for r in queue
            ]
            for fut in as_completed(futs):
                rows.append(fut.result())
    else:
        for r in queue:
            rows.append(_analyze_one(backend, r.stable_id, str(r.path)))

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
    parser.add_argument("--backend", default=DEFAULT_BACKEND)
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


def _is_pair(entry: object) -> bool:
    """A two-element list of two STRINGS, which is the whole handoff schema.

    The member types are checked here and not left to ``Path(...)`` or the
    stable-id handling downstream, because those raise inside ``_dispatch()``
    and ``main()`` translates an escaping exception into
    :data:`EXIT_INTERNAL_ERROR`. A caller that wrote ``[["sid", 1]]`` would
    then read a systemic-fault status for a malformed input file, and the
    chunking caller in the ingest worker classifies those two very
    differently: a usage error is the whole handoff being wrong, while a
    systemic fault stops the run.
    """
    return (
        isinstance(entry, list)
        and len(entry) == 2
        and all(isinstance(member, str) for member in entry)
    )


def _queue_from_pairs(pairs_json: Path) -> tuple[list[TrackRef], int]:
    """Read the handoff file into a queue, and count what it could not admit.

    The handoff file IS this CLI's interface, so everything wrong with it is a
    usage error (:data:`EXIT_USAGE`) rather than an internal fault: a caller
    that wrote a broken one gets told which part, and no chunking caller reads
    it as "some tracks failed".

    The second element is how many named targets were gone by the time
    ``build_queue_from_pairs`` checked. Not an error here - the caller decides
    what a shortfall means - but never zero silently.
    """
    try:
        raw = json.loads(pairs_json.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("--pairs-json %s: %s", pairs_json, exc)
        raise SystemExit(EXIT_USAGE) from exc
    if not isinstance(raw, list) or not all(_is_pair(e) for e in raw):
        log.error("--pairs-json %s: expected [[stable_id, path], ...]", pairs_json)
        raise SystemExit(EXIT_USAGE)
    pairs = [(sid, path) for sid, path in raw]
    queue = build_queue_from_pairs(pairs)
    return queue, len(pairs) - len(queue)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Translates an escaping exception into a status.

    The only broad catch in this module, and it is here because this is the
    boundary where an exception stops being a Python object and becomes an
    exit status that a caller has to classify. ``log.exception`` emits the
    full traceback before the translation, so this hides nothing; it only
    stops CPython from reporting a systemic fault as exit 1.

    ``SystemExit`` derives from ``BaseException`` and passes straight
    through, so the usage exits below keep their own status.
    """
    args = _parse_args(argv)
    try:
        return _dispatch(args)
    except Exception:
        log.exception(
            "the analysis run came apart before any per-track verdict was "
            "reached; this is a fault of this process or this machine, not "
            "of the queued files, so the caller must stop rather than retry "
            "the remaining chunks into the same fault"
        )
        return EXIT_INTERNAL_ERROR


def _dispatch(args: argparse.Namespace) -> int:
    # Resolve the backend BEFORE the queue. An unknown name is a
    # configuration error, and letting it reach _analyze_one turns it into a
    # per-track KeyError repeated once per file, which reads to a chunking
    # caller as "these files failed" and invites it to try the next chunk
    # against the same bad name.
    try:
        get_backend(args.backend)
    except KeyError as exc:
        log.error("--backend %r: %s", args.backend, exc.args[0])
        raise SystemExit(EXIT_USAGE) from exc
    if args.pairs_json is not None:
        queue, dropped = _queue_from_pairs(args.pairs_json)
    else:
        queue = build_queue(
            [Path(p) for p in args.files], strategy=args.stable_id_strategy
        )
        dropped = 0
    summary = run(
        queue,
        backend_name=args.backend,
        dry_run=args.dry_run,
        limit=args.limit,
        workers=args.workers,
        only_missing=args.only_missing,
        verbose=args.verbose,
    )
    # Admission shortfall outranks the verdict on the targets that WERE
    # admitted, and the order matters because only EXIT_MISSING_TARGETS makes
    # the ingest worker clear `queue_signature`. A chunk that lost a target
    # while the analysis install was also broken would otherwise report 4,
    # the worker would book the original snapshot, and the dropped target -
    # never attempted - would sit behind an `unchanged` verdict for as long
    # as it kept its content token.
    #
    # A target can be lost at either of two moments and both mean the same
    # thing here: `dropped` failed the admission check, `vanished` passed it
    # and was gone when the backend opened it. The second is the narrower
    # race, and it used to arrive as a per-track decode failure (exit 1),
    # which books the queue as attempted. A file restored byte-identically
    # then keeps its content token, so the signature never moves and the
    # watcher answers `unchanged` for a track nothing ever analyzed.
    vanished = vanished_count(summary)
    if dropped or vanished:
        log.error(
            "%d of %d target(s) were gone before they could be analyzed "
            "(%d never admitted, %d lost after admission); the caller must "
            "retry rather than record this queue as attempted",
            dropped + vanished, dropped + len(queue), dropped, vanished,
        )
        return EXIT_MISSING_TARGETS
    if backend_never_ran(summary):
        log.error(
            "the %r backend could not run at all (%d/%d targets failed with "
            "BackendNotAvailable), so nothing here was analyzed; this is a "
            "capability failure on this machine, not a property of these "
            "files, and the caller must stop rather than retry per chunk",
            args.backend, summary.failed, summary.failed,
        )
        return EXIT_BACKEND_UNAVAILABLE
    return 0 if summary.failed == 0 else EXIT_TRACK_FAILURES


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
