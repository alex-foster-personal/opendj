"""Drain a persisted backfill batch across a spawned worker pool.

Spec `specs/native-analysis-v1.md` sections 3 and 4; lane brief
`specs/native-analysis-v1-lanes/nav1-queue.md` item 4. Requirement NATIVE-10.

The one invariant everything else here exists to protect:

    THE RECORD WRITE AND THE ITEM'S MOVE TO ``done`` ARE ONE TRANSACTION.

Because they are, a kill can land in exactly two places and no third:

* before the commit -- the record is absent and the item is still
  ``running``. A fresh process returns it to ``pending`` and runs it once.
* after the commit -- the record is present and the item is ``done``. A
  fresh process never touches it, and never counts it twice.

There is no window in which a record exists under an item that still reads
as pending (which would double-count on resume) and none in which an item
reads as done with no record behind it (a torn record: partial write visible
as complete). That is what makes the process-restart arm of the resume test
a real test rather than a same-process one wearing a costume.

Concurrency comes from the batch row, which the admission rule wrote. This
module never picks a worker count of its own, and a batch whose plan says
zero workers runs nothing rather than defaulting to one.

-Claude
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field

from . import queue_store
from .backends.base import AnalyzerBackend
from .canonical import canonical_pointer
from .pool import analyze_one
from .queue import CascadeOutcome, QueueError, cascade_dependents, enqueue
from .record import AnalysisRecord
from .store import upsert_record
from .worker_diagnostics import init_worker

log = logging.getLogger("apps.analysis.queue_runner")


@dataclass
class RunSummary:
    """What one drain did. Every number here is counted, never inferred."""

    batch_id: str
    runner_id: str
    workers: int
    released_on_takeover: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled_midway: bool = False
    cascade_outcomes: list[CascadeOutcome] = field(default_factory=list)
    cascade_batch_id: str | None = None


def _batch_is_cancelled(conn: sqlite3.Connection, batch_id: str) -> bool:
    batch = queue_store.get_batch(conn, batch_id)
    if batch is None:
        raise QueueError(f"batch {batch_id!r} vanished mid-run")
    return batch.state == queue_store.BATCH_CANCELLED


def _already_current(
    conn: sqlite3.Connection, stable_id: str, backend: str, version: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM analysis WHERE stable_id = ? AND backend = ? "
        "AND backend_version = ?",
        (stable_id, backend, version),
    ).fetchone()
    return row is not None


def _commit_record(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    item: queue_store.QueueItem,
    record: AnalysisRecord,
) -> list[CascadeOutcome]:
    """Write the record and finish the item in ONE transaction.

    Returns the cascade outcomes the write produced, computed inside the
    same transaction so a dependent lane can never be left un-re-queued by a
    crash between the write and the cascade.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        upsert_record(record, conn=conn)
        outcomes: list[CascadeOutcome] = []
        if canonical_pointer(conn, item.stable_id, item.lane) == (
            record.backend,
            record.backend_version,
        ):
            outcomes = cascade_dependents(
                conn,
                stable_id=item.stable_id,
                lane=item.lane,
                dependency_record=record,
            )
        queue_store.clear_stale(
            conn,
            stable_id=item.stable_id,
            lane=item.lane,
            backend=record.backend,
            backend_version=record.backend_version,
        )
        queue_store.finish_item(
            conn,
            batch_id=batch_id,
            stable_id=item.stable_id,
            lane=item.lane,
            state=queue_store.ITEM_DONE,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return outcomes


@dataclass
class _RunContext:
    """Everything one drain's inner loop needs, gathered once.

    Exists so the claim loop and the completion handler are two short
    functions rather than one long one; the fields are the drain's identity,
    not options.
    """

    conn: sqlite3.Connection
    batch_id: str
    backend_cls: type[AnalyzerBackend]
    backend_name: str
    version: str
    runner_id: str
    workers: int
    summary: RunSummary
    on_item: Callable[[queue_store.QueueItem, str], None] | None


def _fill_pool(
    ctx: _RunContext,
    pool: ProcessPoolExecutor,
    inflight: dict[Future, queue_store.QueueItem],
) -> None:
    """Claim and submit until the pool is full or the queue is empty.

    An item whose record already exists at the CURRENT producer version is
    settled as ``skipped`` here rather than submitted: that is the idempotent
    re-run promise, and settling it at claim time means the work is never
    started, not started and discarded.
    """
    while len(inflight) < ctx.workers:
        item = queue_store.claim_next(
            ctx.conn, ctx.batch_id, runner_id=ctx.runner_id
        )
        if item is None:
            return
        if _already_current(
            ctx.conn, item.stable_id, ctx.backend_name, ctx.version
        ):
            queue_store.finish_item(
                ctx.conn,
                batch_id=ctx.batch_id,
                stable_id=item.stable_id,
                lane=item.lane,
                state=queue_store.ITEM_SKIPPED,
                reason=queue_store.SKIP_ALREADY_CURRENT,
            )
            ctx.summary.skipped += 1
            if ctx.on_item is not None:
                ctx.on_item(item, queue_store.ITEM_SKIPPED)
            continue
        inflight[
            pool.submit(
                analyze_one, ctx.backend_cls, item.stable_id, item.file_path
            )
        ] = item


def _settle_one(
    ctx: _RunContext, fut: Future, item: queue_store.QueueItem
) -> None:
    """Turn one finished future into a terminal item state."""
    _, record, error = fut.result()
    if error is not None:
        queue_store.finish_item(
            ctx.conn,
            batch_id=ctx.batch_id,
            stable_id=item.stable_id,
            lane=item.lane,
            state=queue_store.ITEM_FAILED,
            reason=error,
        )
        ctx.summary.failed += 1
        if ctx.on_item is not None:
            ctx.on_item(item, queue_store.ITEM_FAILED)
        return
    if record is None:
        raise QueueError(
            f"analyze_one returned neither a record nor an error for "
            f"{item.stable_id}/{item.lane}"
        )
    ctx.summary.cascade_outcomes.extend(
        _commit_record(ctx.conn, batch_id=ctx.batch_id, item=item, record=record)
    )
    ctx.summary.completed += 1
    if ctx.on_item is not None:
        ctx.on_item(item, queue_store.ITEM_DONE)


def run_batch(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    backend_cls: type[AnalyzerBackend],
    runner_id: str | None = None,
    on_item: Callable[[queue_store.QueueItem, str], None] | None = None,
    cascade_resolver: Callable[..., Sequence[object]] | None = None,
) -> RunSummary:
    """Drain ``batch_id`` until it is empty or cancelled.

    ``backend_cls`` is the CLASS, not a name: a spawned worker imports it by
    reference rather than re-reading a registry only the parent populated
    (the reason :func:`apps.analysis.pool.run_pool` does the same).
    """
    queue_store.ensure_queue_tables(conn)
    batch = queue_store.get_batch(conn, batch_id)
    if batch is None:
        raise QueueError(f"no such batch {batch_id!r}")
    rid = runner_id or queue_store.new_runner_id()
    summary = RunSummary(batch_id=batch_id, runner_id=rid, workers=batch.workers)

    # Fresh-process takeover: anything a dead runner was holding was never
    # committed, so it goes back in the queue exactly once.
    summary.released_on_takeover = queue_store.release_running_items(conn, batch_id)
    if batch.state == queue_store.BATCH_CANCELLED:
        raise QueueError(
            f"batch {batch_id!r} is cancelled; resume it before running it"
        )
    if batch.workers < 1:
        queue_store.set_batch_state(conn, batch_id, queue_store.BATCH_DONE)
        return summary
    queue_store.set_batch_state(conn, batch_id, queue_store.BATCH_RUNNING)

    version = backend_cls.version
    backend_name = backend_cls.name
    pool = ProcessPoolExecutor(
        max_workers=batch.workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=init_worker,
    )
    ctx = _RunContext(
        conn=conn,
        batch_id=batch_id,
        backend_cls=backend_cls,
        backend_name=backend_name,
        version=version,
        runner_id=rid,
        workers=batch.workers,
        summary=summary,
        on_item=on_item,
    )
    inflight: dict[Future, queue_store.QueueItem] = {}
    try:
        while True:
            if _batch_is_cancelled(conn, batch_id):
                summary.cancelled_midway = True
                break
            _fill_pool(ctx, pool, inflight)
            if not inflight:
                break
            done, _ = wait(set(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                _settle_one(ctx, fut, inflight.pop(fut))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if summary.cancelled_midway:
        return summary
    counts = queue_store.counts_by_state(conn, batch_id)
    if counts[queue_store.ITEM_PENDING] == 0 and counts[queue_store.ITEM_RUNNING] == 0:
        queue_store.set_batch_state(conn, batch_id, queue_store.BATCH_DONE)
    if cascade_resolver is not None:
        summary.cascade_batch_id = _enqueue_cascade(
            conn, summary.cascade_outcomes, cascade_resolver
        )
    return summary


def _enqueue_cascade(
    conn: sqlite3.Connection,
    outcomes: Sequence[CascadeOutcome],
    resolver: Callable[..., Sequence[object]],
) -> str | None:
    """Enqueue one follow-up batch for everything the cascade invalidated."""
    requeued = [o for o in outcomes if o.requeued]
    if not requeued:
        return None
    lanes = {o.lane for o in requeued}
    if len(lanes) != 1:
        raise QueueError(
            f"cascade spans lanes {sorted(lanes)}; enqueue one batch per lane "
            "so each is budgeted under its own memory model"
        )
    lane = next(iter(lanes))
    candidates = resolver(
        conn, sorted({o.stable_id for o in requeued}), lane=lane
    )
    if not candidates:
        return None
    return enqueue(
        conn,
        list(candidates),  # type: ignore[arg-type]
        note=f"dependency cascade: {lane} re-queued after its dependency moved",
    ).batch_id


def drain(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    backend_for_lane: Callable[[str], type[AnalyzerBackend]],
    cascade_resolver: Callable[..., Sequence[object]] | None = None,
    max_batches: int = 8,
) -> list[RunSummary]:
    """Run a batch and every follow-up batch its cascade produced.

    Bounded by ``max_batches`` and raises when the bound is hit: an
    unbounded loop over a cascade that keeps re-queueing itself is a hang,
    and a hang reported as success is worse than a raise.
    """
    summaries: list[RunSummary] = []
    next_batch: str | None = batch_id
    while next_batch is not None:
        if len(summaries) >= max_batches:
            raise QueueError(
                f"cascade did not settle within {max_batches} batches; "
                f"last batch {next_batch}"
            )
        items = queue_store.list_items(conn, next_batch, limit=1)
        if not items:
            break
        summary = run_batch(
            conn,
            next_batch,
            backend_cls=backend_for_lane(items[0].lane),
            cascade_resolver=cascade_resolver,
        )
        summaries.append(summary)
        next_batch = summary.cascade_batch_id
    return summaries


def pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` still exists. Used by the CLI's takeover guard."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by somebody else.
        return True
    return True


__all__ = [
    "RunSummary",
    "drain",
    "pid_is_alive",
    "run_batch",
]
