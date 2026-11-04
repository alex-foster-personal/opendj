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
``2``  ``EXIT_USAGE`` -- bad flags, an unknown backend, a backend refused for
       licensing reasons (NATIVE-08; e.g. ``librosa+madmom`` without
       ``MDT_BENCH_NONSHIPPABLE=1``), a malformed handoff file. Argparse's
       own convention, kept distinct from ``1`` so a configuration mistake
       is not read as "some tracks failed".
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
import sqlite3
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.shared.state import analysis_stale

from ._warmup_lock import ensure_owned_numba_cache_dir
from .backends import DEFAULT_BACKEND, get_backend
from .backends.base import BackendNonshippable, BackendNotAvailable, TrackVanished
from .bpm_status import bpm_status_for
from .jit_warmup import warm_backend_jit
from .pool import analyze_one, run_pool
from .record import AnalysisRecord
from .source_bytes import StaleSourceBytesError, assert_source_bytes_unchanged, read_track_audio_hash
from .store import UpsertResult, fetch_records_by_ids, open_conn, upsert_record

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
    alternate_paths: tuple[Path, ...] = ()

    @property
    def candidate_paths(self) -> tuple[Path, ...]:
        return (self.path, *self.alternate_paths)


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
        sid = (
            stable_id_from_audio_bytes(p)
            if strategy == "sha256"
            else stable_id_from_path(p)
        )
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


def build_queue_from_candidates(
    entries: Iterable[tuple[str, Sequence[str]]],
) -> list[TrackRef]:
    """Queue from caller-supplied canonical ``(stable_id, [path, ...])`` lists."""
    refs: list[TrackRef] = []
    for sid, paths in entries:
        if not sid:
            raise ValueError("empty stable_id in candidates input")
        if not paths:
            raise ValueError(f"empty candidate list for {sid!r}")
        ordered = [Path(p) for p in paths]
        if not any(p.exists() for p in ordered):
            log.warning("skip (missing): all candidates for %s", sid)
            continue
        refs.append(
            TrackRef(
                stable_id=sid,
                path=ordered[0],
                alternate_paths=tuple(ordered[1:]),
            )
        )
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
    """Drop tracks that already have a current analysis row for ``backend_name``.

    A row retired by an audio rewrite (a whole-record stale marker) describes
    other bytes, so its track is still missing analysis and is queued.
    """
    if not refs:
        return refs
    existing = fetch_records_by_ids(
        [r.stable_id for r in refs], backend=backend_name, db_path=db_path
    )
    conn = open_conn(db_path)
    try:
        retired = analysis_stale.retired_rows(conn, backend_name)
    finally:
        conn.close()
    seen = {r.stable_id for r in existing if (r.stable_id, r.backend_version) not in retired}
    return [r for r in refs if r.stable_id not in seen]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    analysed: int = 0
    skipped_existing: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def _claimed_audio_hashes(queue: list[TrackRef], db_path: Path | None) -> dict[str, str | None]:
    """Each queued track's stored audio hash, read once on a short-lived connection."""
    conn = open_conn(db_path)
    try:
        return {r.stable_id: read_track_audio_hash(conn, r.stable_id) for r in queue}
    finally:
        conn.close()


def upsert_unless_source_moved(
    conn: sqlite3.Connection,
    stable_id: str,
    record: AnalysisRecord,
    claimed_audio_hash: str | None,
    *,
    db_path: Path | None,
) -> UpsertResult:
    """Check the claimed ``audio_hash`` and write ``record`` in ONE write transaction (#6114).

    The shared connection is autocommit, so without ``BEGIN IMMEDIATE`` a folder
    rescan could commit a byte replacement between the check and the upsert, and
    the stale record would be written back after ingest dropped its analysis.
    Same shape as the queue runner's ``_commit_record``.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert_source_bytes_unchanged(conn, stable_id, claimed_audio_hash)
        result = upsert_record(record, db_path=db_path, conn=conn)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return result


def run(  # noqa: C901, PLR0912
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

    # Compile the backend's cached JIT paths HERE, in the parent, before any
    # second process exists. Concurrent cold compiles into one shared numba
    # cache corrupt it, and every later process that loads the corrupt cache
    # dies at a NULL instruction pointer with no traceback (issue #1316). Not
    # behind a flag and not conditional on `workers`: it is the precondition
    # that makes the rest of this function safe to run at all, and the crash
    # reproduces at --workers 1 with no child process in sight.
    #
    # purge_stale=True: this call, not the CI warm-up CLI, is the one that
    # actually stands between a corrupted cache and this process loading it
    # (issue #1572 review). A content mismatch means something wrote to the
    # cache since the last known-good stamp - a racing or killed writer, a
    # torn artifact - and must never merely fall through to warm_jit_cache(),
    # which can still load what just failed to vouch. Scoped to a directory
    # this process verifiably owns (see ensure_owned_numba_cache_dir): an
    # unset NUMBA_CACHE_DIR gets a private one provisioned so every
    # deployment keeps purge protection, and an operator-set one is verified
    # rather than trusted, so purge never fires against a cache this process
    # does not own.
    owned_cache = ensure_owned_numba_cache_dir()
    warmup = warm_backend_jit(
        backend, backend_name=backend_name, purge_stale=owned_cache is not None
    )
    console.print(f"[cyan]{warmup.render()}[/cyan]")
    if warmup.refused:
        raise RuntimeError(
            f"jit warm-up refused ({warmup.detail}); analysis would compile a "
            "cold numba cache unlocked (#1316 / #7227)"
        )

    # The bytes each track had when this batch claimed it, read BEFORE either
    # dispatch path runs: a folder rescan that rewrites a file mid-analysis
    # changes the stored hash, and a snapshot taken after the analysis would
    # read the replacement and accept the stale record.
    claimed_hashes = {} if dry_run else _claimed_audio_hashes(queue, db_path)

    if workers > 1:
        rows.extend(
            run_pool(
                (
                    (r.stable_id, str(r.path), tuple(str(p) for p in r.alternate_paths))
                    for r in queue
                ),
                backend=backend,
                workers=workers,
            )
        )
    else:
        rows.extend(
            analyze_one(
                backend,
                r.stable_id,
                str(r.path),
                tuple(str(p) for p in r.alternate_paths),
            )
            for r in queue
        )

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
                try:
                    result = upsert_unless_source_moved(
                        shared_conn, sid, rec, claimed_hashes.get(sid), db_path=db_path
                    )
                except StaleSourceBytesError as exc:
                    summary.failed += 1
                    summary.errors.append((sid, str(exc)))
                    table.add_row(sid[:14], "-", "-", "-", "-", f"[red]{exc}[/red]")
                    continue
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
            bpm_status, _ = bpm_status_for(rec.bpm, rec.duration_s, rec.features_blob)
            table.add_row(
                sid[:14],
                f"{rec.bpm:.1f}" if bpm_status == "ok" else bpm_status,
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
    source.add_argument(
        "--candidates-json", type=Path,
        help=(
            "JSON file [[stable_id, [path, ...]], ...] carrying ordered "
            "decode candidates for each canonical stable_id."
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


def _is_candidate_entry(entry: object) -> bool:
    """A canonical stable_id plus a non-empty list of path strings."""
    return (
        isinstance(entry, list)
        and len(entry) == 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], list)
        and len(entry[1]) >= 1
        and all(isinstance(path, str) for path in entry[1])
    )


def _queue_from_candidates(candidates_json: Path) -> tuple[list[TrackRef], int]:
    try:
        raw = json.loads(candidates_json.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("--candidates-json %s: %s", candidates_json, exc)
        raise SystemExit(EXIT_USAGE) from exc
    if not isinstance(raw, list) or not all(_is_candidate_entry(e) for e in raw):
        log.error(
            "--candidates-json %s: expected [[stable_id, [path, ...]], ...]",
            candidates_json,
        )
        raise SystemExit(EXIT_USAGE)
    entries = [(sid, paths) for sid, paths in raw]
    queue = build_queue_from_candidates(entries)
    return queue, len(entries) - len(queue)


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
    # configuration error, and letting it reach analyze_one turns it into a
    # per-track KeyError repeated once per file, which reads to a chunking
    # caller as "these files failed" and invites it to try the next chunk
    # against the same bad name.
    try:
        get_backend(args.backend)
    except (KeyError, BackendNonshippable) as exc:
        log.error("--backend %r: %s", args.backend, exc.args[0])
        raise SystemExit(EXIT_USAGE) from exc
    if args.candidates_json is not None:
        queue, dropped = _queue_from_candidates(args.candidates_json)
    elif args.pairs_json is not None:
        queue, dropped = _queue_from_pairs(args.pairs_json)
    else:
        queue = build_queue(
            [Path(p) for p in args.files], strategy=args.stable_id_strategy
        )
        dropped = 0
    try:
        summary = run(
            queue,
            backend_name=args.backend,
            dry_run=args.dry_run,
            limit=args.limit,
            workers=args.workers,
            only_missing=args.only_missing,
            verbose=args.verbose,
        )
    except BackendNotAvailable as exc:
        # The JIT warm-up drives the backend once in the parent before any
        # track (#1316), so a broken analysis install now arrives HERE, as an
        # exception, instead of as a per-track BackendNotAvailable verdict.
        # Same fact, so it gets the same status, and the same precedence
        # under a lost target. Left to main() it read as 5, which outranked
        # EXIT_MISSING_TARGETS and booked a queue nothing attempted (#8243).
        log.error(
            "the %r backend could not run at all (%s), so nothing here was "
            "analyzed; this is a capability failure on this machine, not a "
            "property of these files, and the caller must stop rather than "
            "retry per chunk",
            args.backend, exc,
        )
        if dropped:
            _log_missing_targets(dropped, 0, dropped + len(queue))
            return EXIT_MISSING_TARGETS
        return EXIT_BACKEND_UNAVAILABLE
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
        _log_missing_targets(dropped, vanished, dropped + len(queue))
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


def _log_missing_targets(dropped: int, vanished: int, requested: int) -> None:
    log.error(
        "%d of %d target(s) were gone before they could be analyzed "
        "(%d never admitted, %d lost after admission); the caller must "
        "retry rather than record this queue as attempted",
        dropped + vanished, requested, dropped, vanished,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
