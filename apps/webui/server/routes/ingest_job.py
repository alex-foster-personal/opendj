"""The refresh job itself: its state, its one slot, and how it runs a CLI.

Split out of routes/ingest.py, which owns the ROUTES. This module owns the
job those routes start: the record a worker mutates, the single-slot registry
that makes a second concurrent refresh a 409, and the subprocess runner every
step goes through.

The split is a size limit made useful. ingest.py sat at exactly the 600-line
ceiling, so the analysis step could not grow a capability-failure branch
without breaking it, and these four things are the part with no route in
them: nothing here imports FastAPI or touches a request.

Import direction is one-way - ingest.py imports from here, never the reverse -
which is also why ``_RefreshJob`` moved rather than being type-imported: a
TYPE_CHECKING import back into ingest.py would be a cycle wearing a hat.
"""
from __future__ import annotations

import sqlite3
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from apps.analysis import run as analysis_run
from apps.lyrics import cache as lyrics_cache
from apps.shared import platform_paths
from apps.shared.paths import PROJECT_ROOT
from apps.shared.state import locations as state_locations
from apps.stems.artifacts import (
    StemArtifactError,
    StemBundleNotFoundError,
    load_stem_bundle,
)
from apps.vocals import cache as vocals_cache
from apps.webui.server.routes import ingest_cli_procs
from apps.webui.server.routes.ingest_analysis_argv import CliFailed
from apps.webui.soft_deletes import has_soft_deletes

#: Exit codes that describe THESE targets rather than this machine. Only
#: these may leave the chunk loop running; see :func:`_step_analysis`. The
#: missing-target code is here because that run still analyzed everything it
#: did admit - it reports a shortfall, it does not report a wall.
_PER_TARGET_EXITS: frozenset[int] = frozenset({
    analysis_run.EXIT_TRACK_FAILURES,
    analysis_run.EXIT_MISSING_TARGETS,
})


def _systemic_message(exc: CliFailed, total: int, backend: str) -> str:
    """Why the whole step stopped, in terms a user can act on."""
    if exc.returncode == analysis_run.EXIT_BACKEND_UNAVAILABLE:
        return (
            f"analysis stopped: the {backend!r} backend is not "
            f"installed here, so none of the {total} target(s) could have "
            "been analyzed. Install the analysis extra "
            "(music-dj-tools[analysis]) and run this again."
        )
    return (
        f"analysis stopped after a failure that is not about these files "
        f"({exc}); the remaining targets of {total} were not attempted "
        "because every chunk would meet the same wall. See the job log."
    )


#: Lines of subprocess output kept per job. A ring, so a library-scale run
#: cannot grow the status payload without bound.
LOG_RING: int = 400

#: The phases in which a job still owns the one slot. Everything it publishes
#: - counters, log ring, ``queue_signature`` - is still moving while its phase
#: is one of these, so a reader that needs a FINAL value has to wait for a
#: phase outside the set.
ACTIVE_PHASES: tuple[str, ...] = ("queued", "running")

#: The scope whose jobs publish a ``queue_signature``. Library and batch jobs
#: never target the unmapped backlog, so their silence about it means nothing;
#: an unmapped job's silence is a statement, and only the scope tells them
#: apart.
UNMAPPED_SCOPE: str = "unmapped"


class RefreshStatusOut(BaseModel):
    """The one-slot job's public, serializable state."""

    running: bool
    phase: str
    steps: list[str]
    current_step: str | None
    step_done: int
    step_total: int
    steps_completed: list[str]
    started_at: float | None
    finished_at: float | None
    error: str | None
    log_tail: list[str]
    recently_done_ids: list[str]


@dataclass
class _RefreshJob:
    started_at: float
    steps: list[str]
    batch_dir: Path | None = None
    #: "library" (whole sweep) | "batch" (staged files) | "unmapped" (no
    #: rekordbox mapping). Batch is implied by ``batch_dir``.
    scope: str = "library"
    #: Enabled steps this scope cannot run, dropped BEFORE the 202 so the
    #: reported ``steps`` are what will run. Reason logged by the worker.
    skipped_steps: list[str] = field(default_factory=list)
    phase: str = "queued"            # queued | running | done | error
    current_step: str | None = None
    step_done: int = 0
    step_total: int = 0
    steps_completed: list[str] = field(default_factory=list)
    error: str | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_RING))
    recently_done_ids: deque = field(default_factory=lambda: deque(maxlen=200))
    queue_signature: str | None = None   # unmapped scope: what the worker read
    finished_at: float | None = None
    #: Explicit one-track requests keyed by the analysis kind the caller asked
    #: for. The worker still uses the one shared analysis CLI, never a UI-only
    #: imitation of an analyzer.
    analysis_orders: dict[str, str] = field(default_factory=dict)


_job_lock = threading.RLock()


class _JOBS:
    """One-slot registry for the running refresh job (no global statements)."""

    current: _RefreshJob | None = None

    #: The most recent unmapped drain, kept BESIDE the one slot.
    #:
    #: The slot is replaceable and says nothing about this backlog once a
    #: library or batch job has taken it. A drain that cleared its
    #: ``queue_signature`` - the CLI reported a target it never admitted, so
    #: the queue must be retried rather than booked - is still making a
    #: statement about the backlog after it has left the slot, and the
    #: reconcile loop has to hear it. Without this the newer clear is
    #: invisible, the loop books an older signature over it, and a target
    #: restored byte-identically sits behind ``unchanged`` for good.
    #:
    #: Assigned under ``_job_lock`` when the job is created, at the same
    #: moment as ``current``, so it can never lag behind a ``_wait`` on the
    #: status route. It therefore holds RUNNING drains too, and a reader that
    #: needs a final verdict has to check the phase - the same check it owes
    #: ``current``.
    last_unmapped: _RefreshJob | None = None


def _log(job: _RefreshJob, line: str) -> None:
    job.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")


def _run_cli(job: _RefreshJob, argv: list[str]) -> None:
    """Run a pipeline CLI, streaming stdout into the job log. Raises on rc!=0.

    Raises :class:`CliFailed`, which carries the exit code: callers have to
    tell "this file could not be analyzed" from "this file was not there" and
    from "no backend could have analyzed anything here".
    """
    # Always name the module. ``argv[:3]`` is ``<python> -m <module>``, so a
    # tail-only slice silently drops it the moment the command grows a flag,
    # and the job log - the only record a user or a test has of what ran -
    # stops saying which CLI it was.
    _log(job, "$ " + " ".join([*argv[1:3], *argv[3:][-6:]]))
    proc = subprocess.Popen(
        argv,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Registered so the engine's shutdown can stop it and its pool workers;
    # see ingest_cli_procs.
    ingest_cli_procs.register(proc)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                _log(job, stripped)
        rc = proc.wait()
    finally:
        ingest_cli_procs.unregister(proc)
    if rc != 0:
        raise CliFailed(f"{argv[2] if len(argv) > 2 else argv[0]} exited {rc}", rc)


def valid_lyrics_ids(lyrics_dir: Path) -> tuple[set[str], set[str]]:
    """(done, corrupt) stable_ids for every lyrics-cache entry on disk.

    Mirrors :func:`valid_stem_bundle_ids`: a filename alone is not done. Goes
    through ``apps.lyrics.cache.load`` -- the same reader
    ``LyricsService.fetch`` uses -- so an empty, truncated or schema-invalid
    write from an interrupted worker is parsed and rejected rather than
    trusted by name. An entry whose own ``stable_id`` field disagrees with
    its filename (the same identity-mismatch ``LyricsService.fetch`` guards
    against) is rejected too: it did not survive a same-name overwrite by a
    different track and does not describe this stable_id.

    Both rejection cases land in ``corrupt``, NOT silently folded into an
    ordinary "missing" verdict: lyrics has no refresh runner (see the
    coverage route), so a corrupt entry here has no repair path at all
    unless the coverage response says so explicitly. The caller still
    treats ``corrupt`` ids as missing for the "needs work" count (a
    malformed write is not done, whatever else it is) -- ``corrupt`` is the
    ADDITIONAL, distinguishing signal the UI surfaces as a real error state
    rather than a quiet "incomplete", so corruption cannot go invisible.
    """
    if not lyrics_dir.is_dir():
        return set(), set()
    done: set[str] = set()
    corrupt: set[str] = set()
    for p in lyrics_dir.glob("*.json"):
        stable_id = p.stem
        try:
            entry = lyrics_cache.load(p)
        except (TypeError, ValueError):
            corrupt.add(stable_id)
            continue
        if entry is None:
            continue
        if entry.stable_id != stable_id:
            corrupt.add(stable_id)
            continue
        done.add(stable_id)
    return done, corrupt


def valid_vocal_ids(vocal_dir: Path, audio_paths: dict[str, Path]) -> tuple[set[str], set[str]]:
    """(done, corrupt) stable_ids for every vocal-cache entry on disk.

    Mirrors :func:`valid_stem_bundle_ids`: goes through
    ``apps.vocals.cache.load_valid_entry`` -- the same reader every other
    consumer in this repo (the CLI, the /anlz merge, routes/vocals.py) uses
    -- instead of trusting a filename. That reader raises ``ValueError`` on
    a genuinely MALFORMED entry (corrupt JSON, missing/invalid fields) and
    returns ``None`` when the entry is merely STALE for the on-disk audio
    (schema bump, or the file was replaced since the entry was written,
    tracked via ``audio_signature``/``signature_matches``).

    Those two are kept distinct on purpose: a stale entry is legitimately
    "missing and repairable" by the existing vocals refresh runner, exactly
    like a track that was never analysed -- it must NOT be reported as
    corruption, or every stale entry starts screaming. Only the raised,
    malformed case lands in ``corrupt``. Both count as missing for the
    "needs work" total; ``corrupt`` is the additional signal that lets the
    UI tell "never run yet" apart from "ran and produced garbage".
    """
    if not vocal_dir.is_dir():
        return set(), set()
    done: set[str] = set()
    corrupt: set[str] = set()
    for p in vocal_dir.glob("*.json"):
        stable_id = p.stem
        audio_path = audio_paths.get(stable_id)
        if audio_path is None:
            continue
        try:
            entry = vocals_cache.load_valid_entry(p, audio_path)
        except ValueError:
            corrupt.add(stable_id)
            continue
        if entry is not None:
            done.add(stable_id)
    return done, corrupt


def valid_stem_bundle_ids(roots: Sequence[Path]) -> tuple[set[str], set[str]]:
    """(done, corrupt) stable_ids for stem-bundle directories under ``roots``.

    A directory alone is not done: an interrupted worker can leave it without
    a manifest or with missing/corrupt stem files, and counting it as covered
    would exclude the track from refresh targets forever. ``load_stem_bundle``
    searches every configured root, so a valid bundle in any root still wins
    (STEM-01) even when another root holds junk. Directories the reader
    rejects, with no valid bundle anywhere, land in ``corrupt``. A track with
    no directory in any root is neither set (missing, never run).
    """
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.iterdir():
            if p.is_dir():
                seen.add(p.name)
    done: set[str] = set()
    for name in seen:
        try:
            load_stem_bundle(name, roots=roots)
        except (StemArtifactError, StemBundleNotFoundError):
            continue
        done.add(name)
    return done, seen - done


def tracks_on_disk(
    conn_factory: Callable[[], sqlite3.Connection],
) -> tuple[list[tuple[str, str]], int]:
    """(stable_id, file_path) for tracks whose file is materialised; + unreachable count.

    ``conn_factory`` is the caller's own DB opener (ingest.py's ``open_ro``,
    monkeypatched in tests) passed in explicitly rather than imported here,
    so this module stays request-DB-agnostic - the same reason ``ingest.py``
    exists as a separate module in the first place (see the module docstring).
    """
    conn = conn_factory()
    try:
        if has_soft_deletes(conn, "tracks"):
            tracks_sql = (
                "SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL"
            )
        else:
            tracks_sql = "SELECT stable_id, file_path FROM tracks"
        rows = conn.execute(tracks_sql).fetchall()
        stable_ids = [str(sid) for sid, _fp in rows]
        track_paths = {str(sid): fp for sid, fp in rows}
        resolved = state_locations.bulk_local_audio_paths(conn, stable_ids)
    finally:
        conn.close()
    ok: list[tuple[str, str]] = []
    unreachable = 0
    for sid in stable_ids:
        path = resolved.get(sid)
        if path is not None:
            ok.append((sid, str(path)))
            continue
        fp = track_paths.get(sid)
        if fp and str(fp).startswith(platform_paths.STREAMING_PREFIXES):
            continue
        unreachable += 1
    return ok, unreachable


def missing_by_step(
    on_disk: list[tuple[str, str]],
    conn_factory: Callable[[], sqlite3.Connection],
    stem_roots: Sequence[Path],
    vocal_dir: Path,
    lyrics_dir: Path,
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]]]:
    """(missing, corrupt) per step. ``corrupt`` is always a subset of
    ``missing`` -- a malformed entry is not done, whatever else it is -- and
    exists only so the coverage route can surface it as a DISTINCT signal
    instead of folding it into an ordinary "not yet run" verdict.

    Same ``conn_factory`` seam as :func:`tracks_on_disk`, for the same reason.
    """
    conn = conn_factory()
    try:
        # apps.analysis.store creates its table on first write, so a library
        # that has never been analysed legitimately has no ``analysis`` table:
        # that means zero tracks analysed, not an error.
        has_analysis = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis'"
        ).fetchone() is not None
        analysed = (
            {r[0] for r in conn.execute("SELECT DISTINCT stable_id FROM analysis")}
            if has_analysis
            else set()
        )
    finally:
        conn.close()
    stems_done, stems_corrupt = valid_stem_bundle_ids(stem_roots)
    # lyrics has no STEPS/refresh runner - this key only feeds the coverage
    # dot on the browser panel.
    audio_paths = {sid: Path(fp) for sid, fp in on_disk}
    vocals_done, vocals_corrupt = valid_vocal_ids(vocal_dir, audio_paths)
    lyrics_done, lyrics_corrupt = valid_lyrics_ids(lyrics_dir)
    missing = {
        "analysis": [(s, f) for s, f in on_disk if s not in analysed],
        "stems": [(s, f) for s, f in on_disk if s not in stems_done],
        "vocals": [(s, f) for s, f in on_disk if s not in vocals_done],
        "lyrics": [(s, f) for s, f in on_disk if s not in lyrics_done],
    }
    corrupt = {
        "analysis": [],
        "stems": [(s, f) for s, f in on_disk if s in stems_corrupt],
        "vocals": [(s, f) for s, f in on_disk if s in vocals_corrupt],
        "lyrics": [(s, f) for s, f in on_disk if s in lyrics_corrupt],
    }
    return missing, corrupt


__all__ = [
    "LOG_RING",
    "_JOBS",
    "_RefreshJob",
    "_job_lock",
    "_log",
    "_run_cli",
    "missing_by_step",
    "tracks_on_disk",
    "valid_lyrics_ids",
    "valid_stem_bundle_ids",
    "valid_vocal_ids",
]
