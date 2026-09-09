"""The background analysis backfill queue: enqueue, progress, cancel, resume.

Spec `specs/native-analysis-v1.md` section 3 ("Queue") and section 4; lane
brief `specs/native-analysis-v1-lanes/nav1-queue.md`; requirement NATIVE-10.

This module is the POLICY half. :mod:`apps.analysis.queue_store` owns the
rows and the state machine; :mod:`apps.analysis.queue_runner` owns the
process pool that drains a batch. Everything a caller can ask the queue to
do is here, once, so the CLI
(:mod:`apps.analysis.queue_cli`), the HTTP router
(``apps/webui/server/routes/analysis_backfill.py``) and the UI panel that
calls that router are three faces of ONE implementation rather than three
implementations that agree today.

Four controls, and the two automatic re-queues:

* **enqueue** -- run the memory admission rule over the candidates, persist
  the plan, and record every refusal with its named reason. A refused track
  is a row in the queue with ``state=refused``, never a silently absent one.
* **progress** -- per-item state plus the aggregate, and the admission
  decision the batch was planned under.
* **cancel** -- pending AND in-flight items go to ``cancelled``. An
  in-flight item's record was never committed (the runner commits the record
  and the item state in ONE transaction), so cancelling it cannot leave a
  torn record.
* **resume** -- cancelled items go back to pending, items a dead runner was
  holding go back to pending, and terminal items are left alone. The batch
  is re-planned, because the admitted set may now be shorter than it was.
* **producer version bump** -- :func:`requeue_for_version_bump` re-queues
  every track whose latest record for a lane is on an OLD producer version,
  and no track already on the new one.
* **dependency cascade** -- :func:`cascade_dependents`. The LANE-LEVEL edge
  list (:data:`LANE_DEPENDENCIES`, today only key -> beatgrid) is static and
  registered independently of any record, so a key job that ran BEFORE any
  beatgrid existed (status ``missing``, reason ``no_own_downbeats``, and
  therefore no ``depends_on`` identity to compare) is still re-queued the
  first time a canonical beatgrid appears. The record-level five-field
  ``depends_on`` block decides whether a SPECIFIC existing record is stale;
  the static edge decides whether the LANE is affected at all.

-Claude
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from . import admission, queue_store
from .depends_on import declared_dependency, dependency_identity, dependency_matches
from .lane_enums import LANES
from .lanes import parse_own_backend
from .record import AnalysisRecord

#: Static lane-level edge list: ``dependent -> lanes it depends on``.
#: Registered independently of any record, which is what lets a lane with no
#: record at all (or a ``missing`` one with no dependency identity) be
#: re-queued when its dependency first appears.
LANE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "key": ("beatgrid",),
}

#: Reverse edges, computed once: ``lane -> lanes that depend on it``.
LANE_DEPENDENTS: dict[str, tuple[str, ...]] = {
    dep: tuple(
        sorted(
            dependent
            for dependent, deps in LANE_DEPENDENCIES.items()
            if dep in deps
        )
    )
    for dep in {d for deps in LANE_DEPENDENCIES.values() for d in deps}
}

#: The reason a key record carries when it ran before any own beatgrid
#: existed. Spec section 5 / nav1-key-record. Such a record has no
#: ``depends_on`` identity, so only the static edge can re-queue it.
NO_OWN_DOWNBEATS: str = "no_own_downbeats"


class QueueError(RuntimeError):
    """The queue was asked for something it cannot do."""


#-----------------------------------------------------------------------------
# enqueue
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class EnqueueResult:
    """What one enqueue call planned."""

    batch_id: str
    admitted: int
    refused: int
    workers: int
    band: str
    model: admission.MemoryModel

    @property
    def offered(self) -> int:
        return self.admitted + self.refused


def _model_dict(model: admission.MemoryModel) -> dict[str, Any]:
    return {
        "backend": model.backend,
        "producer_version": model.producer_version,
        "floor_mb": model.floor_mb,
        "slope_mb_per_min": model.slope_mb_per_min,
        "measured_on": model.measured_on,
        "source": model.source,
    }


def enqueue(
    conn: sqlite3.Connection,
    candidates: Sequence[admission.Candidate],
    *,
    note: str | None = None,
    model: admission.MemoryModel | None = None,
) -> EnqueueResult:
    """Admit, plan and persist a batch.

    ``model`` defaults to the registered model for the FIRST candidate's
    backend. One batch is one producer's work, so budgeting the batch under
    one model is correct; mixing backends in a batch would make the band a
    meaningless average, so it is refused rather than averaged.
    """
    if not candidates:
        raise QueueError("enqueue called with no candidates; nothing to plan")
    backends = {c.backend for c in candidates}
    if len(backends) != 1:
        raise QueueError(
            f"one batch is one producer's work, got backends {sorted(backends)}; "
            "enqueue them as separate batches so each is budgeted under its own "
            "measured memory model"
        )
    for cand in candidates:
        if cand.lane not in LANES:
            raise QueueError(f"unknown lane {cand.lane!r}; lanes are {LANES}")
    backend = next(iter(backends))
    chosen = model if model is not None else admission.memory_model_for(backend)
    plan = admission.admit(candidates, model=chosen)

    batch_id = queue_store.new_batch_id()
    queue_store.ensure_queue_tables(conn)
    queue_store.create_batch(
        conn,
        batch_id=batch_id,
        workers=plan.workers,
        band=plan.band,
        memory_model=_model_dict(plan.model),
        note=note,
    )
    for item in plan.admitted:
        queue_store.add_item(
            conn,
            batch_id,
            queue_store.NewItem(
                stable_id=item.stable_id,
                lane=item.lane,
                backend=item.backend,
                file_path=item.file_path,
                duration_s=item.duration_s,
                predicted_peak_mb=item.predicted_peak_mb,
                state=queue_store.ITEM_PENDING,
            ),
        )
    for refusal in plan.refused:
        queue_store.add_item(
            conn,
            batch_id,
            queue_store.NewItem(
                stable_id=refusal.stable_id,
                lane=refusal.lane,
                backend=refusal.backend,
                file_path="",
                duration_s=refusal.duration_s,
                predicted_peak_mb=refusal.predicted_peak_mb,
                state=queue_store.ITEM_REFUSED,
                reason=f"{refusal.reason}: {refusal.detail}",
            ),
        )
    return EnqueueResult(
        batch_id=batch_id,
        admitted=len(plan.admitted),
        refused=len(plan.refused),
        workers=plan.workers,
        band=plan.band,
        model=plan.model,
    )


#-----------------------------------------------------------------------------
# progress
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class Progress:
    """Aggregate + per-item progress for one batch."""

    batch_id: str
    state: str
    workers: int
    band: str
    memory_model: dict[str, Any]
    counts: dict[str, int]
    items: list[queue_store.QueueItem]
    created_at: str
    updated_at: str

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def settled(self) -> int:
        """Items that will not run again in this batch."""
        return sum(
            self.counts[state] for state in queue_store.TERMINAL_ITEM_STATES
        )


def progress(
    conn: sqlite3.Connection, batch_id: str, *, item_limit: int | None = 200
) -> Progress:
    queue_store.ensure_queue_tables(conn)
    batch = queue_store.get_batch(conn, batch_id)
    if batch is None:
        raise QueueError(f"no such batch {batch_id!r}")
    return Progress(
        batch_id=batch.batch_id,
        state=batch.state,
        workers=batch.workers,
        band=batch.band,
        memory_model=batch.memory_model,
        counts=queue_store.counts_by_state(conn, batch_id),
        items=queue_store.list_items(conn, batch_id, limit=item_limit),
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


#-----------------------------------------------------------------------------
# cancel / resume
#-----------------------------------------------------------------------------

def cancel(conn: sqlite3.Connection, batch_id: str) -> int:
    """Cancel a batch. Returns how many items were still open."""
    queue_store.ensure_queue_tables(conn)
    batch = queue_store.get_batch(conn, batch_id)
    if batch is None:
        raise QueueError(f"no such batch {batch_id!r}")
    cancelled = queue_store.cancel_open_items(conn, batch_id)
    queue_store.set_batch_state(conn, batch_id, queue_store.BATCH_CANCELLED)
    return cancelled


def resume(conn: sqlite3.Connection, batch_id: str) -> int:
    """Put a cancelled or abandoned batch back in the queue.

    Returns how many items became runnable again. Re-plans the batch's
    concurrency from what is LEFT: a batch whose one long track already
    finished should not stay at one worker for its remaining club edits.
    """
    queue_store.ensure_queue_tables(conn)
    batch = queue_store.get_batch(conn, batch_id)
    if batch is None:
        raise QueueError(f"no such batch {batch_id!r}")
    revived = queue_store.revive_cancelled_items(conn, batch_id)
    released = queue_store.release_running_items(conn, batch_id)
    remaining = queue_store.list_items(
        conn, batch_id, states=(queue_store.ITEM_PENDING,), limit=None
    )
    durations = [i.duration_s for i in remaining if i.duration_s]
    if durations:
        longest = max(durations)
        queue_store.set_batch_workers(
            conn,
            batch_id,
            workers=admission.workers_for_longest(longest),
            band=admission.band_for_longest(longest),
        )
    else:
        queue_store.set_batch_workers(
            conn, batch_id, workers=0, band=admission.BAND_EMPTY
        )
    queue_store.set_batch_state(conn, batch_id, queue_store.BATCH_QUEUED)
    return revived + released


#-----------------------------------------------------------------------------
# producer version bump
#-----------------------------------------------------------------------------

def tracks_on_old_version(
    conn: sqlite3.Connection, *, backend: str, current_version: str
) -> list[str]:
    """Tracks with a record for ``backend`` at some OTHER version.

    A track that already has a row at ``current_version`` is excluded even
    when it also has older rows: rows never overwrite across versions, so
    "has the new one" is the question, not "has only the new one".
    """
    rows = conn.execute(
        "SELECT stable_id, backend_version FROM analysis WHERE backend = ?",
        (backend,),
    ).fetchall()
    current: set[str] = set()
    older: set[str] = set()
    for stable_id, version in rows:
        if version == current_version:
            current.add(stable_id)
        else:
            older.add(stable_id)
    return sorted(older - current)


def requeue_for_version_bump(
    conn: sqlite3.Connection,
    *,
    backend: str,
    current_version: str,
    lane: str,
    resolve: TargetResolver,
    note: str | None = None,
) -> EnqueueResult | None:
    """Enqueue every track whose record for ``backend`` predates the bump.

    Returns ``None`` when nothing is stale, so a caller can tell "no work"
    from "one item of work" without reading a count of zero as either.
    """
    stale = tracks_on_old_version(
        conn, backend=backend, current_version=current_version
    )
    if not stale:
        return None
    candidates = resolve(conn, stale, lane=lane, backend=backend)
    if not candidates:
        return None
    return enqueue(
        conn,
        candidates,
        note=note
        or f"producer version bump: {backend} -> {current_version}",
    )


#-----------------------------------------------------------------------------
# dependency cascade
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class CascadeOutcome:
    """One dependent lane's verdict for one track."""

    stable_id: str
    lane: str
    requeued: bool
    reason: str
    staled_backend: str | None = None
    staled_version: str | None = None


def cascade_dependents(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    lane: str,
    dependency_record: AnalysisRecord,
) -> list[CascadeOutcome]:
    """Decide what a change to ``lane``'s canonical record makes stale.

    Called after the canonical pointer for (``stable_id``, ``lane``) moves.
    For every dependent lane:

    * no record at all, or a record whose ``depends_on`` block does not
      match the new dependency identity field for field -> re-queue, and
      mark the existing record stale so it drops out of the canonical
      pointer until it is recomputed;
    * a record whose block matches -> nothing, it is already current.

    Returns one outcome per dependent lane so the caller can report what it
    did rather than an unattributable count.
    """
    from .canonical import canonical_pointer  # local: canonical imports us back

    queue_store.ensure_queue_tables(conn)
    actual = dependency_identity(dependency_record, lane)
    outcomes: list[CascadeOutcome] = []
    for dependent in LANE_DEPENDENTS.get(lane, ()):
        pointer = canonical_pointer(conn, stable_id, dependent)
        if pointer is None:
            outcomes.append(
                CascadeOutcome(
                    stable_id=stable_id,
                    lane=dependent,
                    requeued=True,
                    reason=(
                        f"no canonical {dependent} record; the static "
                        f"{dependent} -> {lane} edge re-queues it now that a "
                        f"canonical {lane} exists"
                    ),
                )
            )
            continue
        dep_backend, dep_version = pointer
        row = conn.execute(
            "SELECT record_json FROM analysis WHERE stable_id = ? AND "
            "backend = ? AND backend_version = ?",
            (stable_id, dep_backend, dep_version),
        ).fetchone()
        if row is None:
            raise QueueError(
                f"canonical pointer for {stable_id}/{dependent} names "
                f"{dep_backend}@{dep_version} but no such record row exists"
            )
        record = AnalysisRecord.from_json(row[0])
        declared = declared_dependency(record, lane)
        if dependency_matches(declared, actual):
            outcomes.append(
                CascadeOutcome(
                    stable_id=stable_id,
                    lane=dependent,
                    requeued=False,
                    reason=f"depends_on block already matches the canonical {lane}",
                )
            )
            continue
        queue_store.mark_stale(
            conn,
            stable_id=stable_id,
            lane=dependent,
            backend=dep_backend,
            backend_version=dep_version,
            reason=queue_store.STALE_DEPENDENCY_MOVED,
        )
        outcomes.append(
            CascadeOutcome(
                stable_id=stable_id,
                lane=dependent,
                requeued=True,
                reason=(
                    "declared depends_on block does not match the canonical "
                    f"{lane} field for field"
                    if declared is not None
                    else f"record declares no depends_on block for {lane}"
                ),
                staled_backend=dep_backend,
                staled_version=dep_version,
            )
        )
    return outcomes


def clear_stale_for_record(
    conn: sqlite3.Connection, record: AnalysisRecord
) -> int:
    """Drop the staleness marker a freshly written record supersedes.

    Called on every own-record write. A recomputed record for the lane makes
    the stale marker on the OLD row irrelevant only in the sense that the new
    row is now the highest-ranked eligible one; the old marker stays, which
    is correct, because that old row really is still stale. What is cleared
    is a marker on the row being written, which happens when a record is
    re-written at the same version after its dependency was restored.
    """
    parsed = parse_own_backend(record.backend)
    if parsed is None:
        return 0
    return queue_store.clear_stale(
        conn,
        stable_id=record.stable_id,
        lane=parsed.lane,
        backend=record.backend,
        backend_version=record.backend_version,
    )


#-----------------------------------------------------------------------------
# target resolution
#-----------------------------------------------------------------------------

class TargetResolver:
    """Callable turning stable_ids into admission candidates.

    A protocol-shaped class rather than a bare function type so the queue's
    version-bump path can be driven with the real state-layer resolver in
    production and with an explicit list in a test, without either one
    reaching into the other's world.
    """

    def __call__(
        self,
        conn: sqlite3.Connection,
        stable_ids: Sequence[str],
        *,
        lane: str,
        backend: str,
    ) -> list[admission.Candidate]:  # pragma: no cover - protocol
        raise NotImplementedError


def candidates_from_state(
    conn: sqlite3.Connection,
    stable_ids: Sequence[str],
    *,
    lane: str,
    backend: str,
) -> list[admission.Candidate]:
    """Resolve stable_ids against the state layer into queue candidates.

    Duration comes from ``tracks.duration_ms``, which is what the library
    already knows; a row with no duration becomes a candidate with
    ``duration_s=None`` and is REFUSED by the admission rule with
    ``duration_unknown`` rather than admitted at an invented length.

    Paths go through the same resolution the backlog drain uses
    (``platform_paths.resolve_library_path`` over the legacy column plus
    ``track_locations``), so the queue and the drain cannot disagree about
    where a track's bytes are.
    """
    from apps.shared import platform_paths
    from apps.shared.state import locations as track_locations

    from .backlog import _candidate_paths

    if not stable_ids:
        return []
    wanted = list(stable_ids)
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT stable_id, file_path, duration_ms FROM tracks "
        f"WHERE stable_id IN ({placeholders})",
        wanted,
    ).fetchall()
    found = {row[0] for row in rows}
    missing = [sid for sid in wanted if sid not in found]
    if missing:
        raise QueueError(
            f"{len(missing)} stable_id(s) are not in tracks: {missing[:5]}"
        )
    path_map = platform_paths.load_path_map()
    locations = track_locations.list_location_paths(conn, wanted)
    out: list[admission.Candidate] = []
    for stable_id, file_path, duration_ms in rows:
        resolved: str | None = None
        for candidate in _candidate_paths(
            file_path, locations.get(stable_id, ())
        ):
            mapped = platform_paths.resolve_library_path(
                candidate, path_map=path_map
            )
            if mapped.resolved is not None:
                resolved = str(mapped.resolved)
                break
        if resolved is None:
            # No local bytes on THIS machine. Offered anyway, with no
            # duration, so the admission rule refuses it by name instead of
            # the queue dropping it silently.
            out.append(
                admission.Candidate(
                    stable_id=stable_id,
                    lane=lane,
                    backend=backend,
                    file_path="",
                    duration_s=None,
                )
            )
            continue
        out.append(
            admission.Candidate(
                stable_id=stable_id,
                lane=lane,
                backend=backend,
                file_path=resolved,
                duration_s=(duration_ms / 1000.0) if duration_ms else None,
            )
        )
    return out


def with_lane(
    candidate: admission.Candidate, *, lane: str, backend: str
) -> admission.Candidate:
    """Same track, different lane. Used when one enqueue covers two lanes."""
    return replace(candidate, lane=lane, backend=backend)


__all__ = [
    "LANE_DEPENDENCIES",
    "LANE_DEPENDENTS",
    "NO_OWN_DOWNBEATS",
    "CascadeOutcome",
    "EnqueueResult",
    "Progress",
    "QueueError",
    "TargetResolver",
    "cancel",
    "candidates_from_state",
    "cascade_dependents",
    "clear_stale_for_record",
    "enqueue",
    "progress",
    "requeue_for_version_bump",
    "resume",
    "tracks_on_old_version",
    "with_lane",
]
