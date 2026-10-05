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
import os
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field

from . import queue_store
from ._warmup_lock import ensure_owned_numba_cache_dir
from .backends.base import AnalyzerBackend, TrackVanished
from .jit_warmup import warm_backend_jit
from .lane_enums import LaneContractError
from .pool import analyze_one, spawn_pool
from .queue import CascadeOutcome, QueueError, enqueue
from .queue_effects import cascade_if_canonical
from .record import AnalysisRecord, RecordContractError
from .store import upsert_record
from .worker_diagnostics import (
    pool_death_message,
    worker_exit_signals,
)

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
    deferred: int = 0
    #: Results thrown away because `/cancel` took the claim while the worker
    #: was still analyzing. Counted, never folded into completed or failed:
    #: the work happened and its output was deliberately discarded.
    discarded_on_cancel: int = 0
    cancelled_midway: bool = False
    cascade_outcomes: list[CascadeOutcome] = field(default_factory=list)
    cascade_batch_id: str | None = None


def _batch_is_cancelled(conn: sqlite3.Connection, batch_id: str) -> bool:
    batch = queue_store.get_batch(conn, batch_id)
    if batch is None:
        raise QueueError(f"batch {batch_id!r} vanished mid-run")
    return batch.state == queue_store.BATCH_CANCELLED


def _already_current(
    conn: sqlite3.Connection, item: queue_store.QueueItem, backend: str, version: str
) -> bool:
    """Is there a usable record for this exact producer version already?

    Two conditions, and the second is the one that is easy to forget: the row
    must exist, AND the queue must not have marked it STALE. A record the
    dependency cascade invalidated exists at the current version and is
    precisely the thing that needs recomputing, so a skip check that only
    asked "does a row exist" would make every cascade re-queue a no-op and
    the dependent lane would never recover.
    """
    row = conn.execute(
        "SELECT 1 FROM analysis WHERE stable_id = ? AND backend = ? "
        "AND backend_version = ?",
        (item.stable_id, backend, version),
    ).fetchone()
    if row is None:
        return False
    return (backend, version) not in queue_store.stale_rows(
        conn, item.stable_id, item.lane
    )


def _commit_record(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    item: queue_store.QueueItem,
    record: AnalysisRecord,
    runner_id: str,
) -> list[CascadeOutcome] | None:
    """Write the record and finish the item in ONE transaction.

    Returns the cascade outcomes the write produced, computed inside the
    same transaction so a dependent lane can never be left un-re-queued by a
    crash between the write and the cascade.

    Returns ``None`` when the claim was taken away while the worker was
    analyzing -- a live ``/cancel`` -- and the whole transaction is rolled
    back. The record is discarded rather than written: a cancel that the
    worker's own settlement could undo is not a cancel, and the item would
    read ``done`` to a ``resume`` that must revisit it.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        # BEFORE the write, not after: upsert_record recomputes the canonical
        # pointer, and it reads analysis_stale while doing so. Clearing the
        # marker afterwards would leave the freshly recomputed record excluded
        # from its own pointer until some later write happened to recompute it
        # again -- the lane would look permanently unanalyzed while carrying a
        # perfectly good record.
        queue_store.clear_stale(
            conn,
            stable_id=item.stable_id,
            lane=item.lane,
            backend=record.backend,
            backend_version=record.backend_version,
        )
        upsert_record(record, conn=conn, cascade=False, version_bump=False)
        outcomes: list[CascadeOutcome] = cascade_if_canonical(conn, record)
        settled = queue_store.finish_item(
            conn,
            batch_id=batch_id,
            stable_id=item.stable_id,
            lane=item.lane,
            state=queue_store.ITEM_DONE,
            claimed_by=runner_id,
        )
        if not settled:
            conn.execute("ROLLBACK")
            return None
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
        if item.backend != ctx.backend_name:
            settled = queue_store.finish_item(
                ctx.conn,
                batch_id=ctx.batch_id,
                stable_id=item.stable_id,
                lane=item.lane,
                state=queue_store.ITEM_FAILED,
                reason=(
                    f"runner backend {ctx.backend_name!r} does not match "
                    f"item backend {item.backend!r}"
                ),
                claimed_by=ctx.runner_id,
            )
            if settled:
                ctx.summary.failed += 1
            continue
        if _already_current(ctx.conn, item, ctx.backend_name, ctx.version):
            settled = queue_store.finish_item(
                ctx.conn,
                batch_id=ctx.batch_id,
                stable_id=item.stable_id,
                lane=item.lane,
                state=queue_store.ITEM_SKIPPED,
                reason=queue_store.SKIP_ALREADY_CURRENT,
                claimed_by=ctx.runner_id,
            )
            if not settled:
                # Cancelled between the claim and the skip check. Same rule
                # as a finished worker: the cancel wins.
                continue
            ctx.summary.skipped += 1
            if ctx.on_item is not None:
                ctx.on_item(item, queue_store.ITEM_SKIPPED)
            continue
        inflight[
            pool.submit(
                analyze_one, ctx.backend_cls, item.stable_id, item.file_path
            )
        ] = item


#: ``analyze_one`` crosses a process boundary, so a per-track failure arrives
#: as ``"<ExceptionName>: <message>"`` and not as the exception. This is the
#: one class of failure in that string that is about the MACHINE rather than
#: the track: the analysis dependency closure, a model or a resampler is
#: absent, and every remaining item in the batch would meet it too. Recording
#: it per track would write a capability fault as a terminal per-track verdict
#: that ``resume`` can never undo.
_BACKEND_UNAVAILABLE: str = "BackendNotAvailable:"
_TRACK_VANISHED: str = f"{TrackVanished.__name__}:"


class BackendUnavailable(QueueError):
    """The batch stopped because the backend itself is not installable here.

    Separate from :class:`QueueError` so a caller can tell "this host cannot
    run this producer, fix the host and resume" from "this batch is
    malformed". Nothing is marked failed: the item that met it is back in
    ``pending`` and the rest were never attempted.
    """


def _settle_one(
    ctx: _RunContext, fut: Future, item: queue_store.QueueItem
) -> None:
    """Turn one finished future into a terminal item state."""
    _, record, error = fut.result()
    if error is not None:
        if error.startswith(_BACKEND_UNAVAILABLE):
            # Every sibling claim goes back too. They meet the same missing
            # capability, and a claim left `running` is only recovered by a
            # fresh-process takeover -- which is not what `resume` does, so
            # the batch would look permanently half-held.
            released = queue_store.release_running_items(ctx.conn, ctx.batch_id)
            queue_store.set_batch_state(
                ctx.conn, ctx.batch_id, queue_store.BATCH_QUEUED
            )
            raise BackendUnavailable(
                f"backend {ctx.backend_name!r} is not available on this host, "
                f"so batch {ctx.batch_id} stopped with {released} item(s) "
                f"returned to pending and nothing marked failed: {error}"
            )
        terminal_state = (
            queue_store.ITEM_DEFERRED
            if error.startswith(_TRACK_VANISHED)
            else queue_store.ITEM_FAILED
        )
        settled = queue_store.finish_item(
            ctx.conn,
            batch_id=ctx.batch_id,
            stable_id=item.stable_id,
            lane=item.lane,
            state=terminal_state,
            reason=error,
            claimed_by=ctx.runner_id,
        )
        if not settled:
            ctx.summary.discarded_on_cancel += 1
            return
        if terminal_state == queue_store.ITEM_DEFERRED:
            ctx.summary.deferred += 1
        else:
            ctx.summary.failed += 1
        if ctx.on_item is not None:
            ctx.on_item(item, terminal_state)
        return
    if record is None:
        raise QueueError(
            f"analyze_one returned neither a record nor an error for "
            f"{item.stable_id}/{item.lane}"
        )
    try:
        outcomes = _commit_record(
            ctx.conn,
            batch_id=ctx.batch_id,
            item=item,
            record=record,
            runner_id=ctx.runner_id,
        )
    except (RecordContractError, LaneContractError) as exc:
        # The producer emitted a record the contract refuses for THIS track
        # (live: a fitted beat 0.011 s past a frame-derived duration). The
        # commit rolled back, so the item fails with the breach as its reason
        # and the batch carries on; raising here took the whole drain down
        # and stranded every sibling claim as `running`.
        _settle_failed(ctx, item, f"{type(exc).__name__}: {exc}")
        return
    if outcomes is None:
        # Cancelled while this worker was analyzing. The record was rolled
        # back with the settlement, so the item stays cancelled and a resume
        # runs it again rather than reading a done it never asked for.
        ctx.summary.discarded_on_cancel += 1
        return
    ctx.summary.cascade_outcomes.extend(outcomes)
    ctx.summary.completed += 1
    if ctx.on_item is not None:
        ctx.on_item(item, queue_store.ITEM_DONE)


def _settle_failed(ctx: _RunContext, item: queue_store.QueueItem, reason: str) -> None:
    settled = queue_store.finish_item(
        ctx.conn,
        batch_id=ctx.batch_id,
        stable_id=item.stable_id,
        lane=item.lane,
        state=queue_store.ITEM_FAILED,
        reason=reason,
        claimed_by=ctx.runner_id,
    )
    if not settled:
        ctx.summary.discarded_on_cancel += 1
        return
    ctx.summary.failed += 1
    if ctx.on_item is not None:
        ctx.on_item(item, queue_store.ITEM_FAILED)


def _snapshot_workers(
    pool: ProcessPoolExecutor, seen: dict[int, object]
) -> None:
    """Record the pool's worker processes while they are still reachable."""
    seen.update(
        {p.pid: p for p in (getattr(pool, "_processes", None) or {}).values()}
    )


def _pump_pool(
    ctx: _RunContext, pool: ProcessPoolExecutor, seen: dict[int, object]
) -> None:
    """Claim, submit and settle until the batch is empty or cancelled."""
    inflight: dict[Future, queue_store.QueueItem] = {}
    while True:
        if _batch_is_cancelled(ctx.conn, ctx.batch_id):
            ctx.summary.cancelled_midway = True
            return
        _fill_pool(ctx, pool, inflight)
        _snapshot_workers(pool, seen)
        if not inflight:
            return
        done, _ = wait(set(inflight), return_when=FIRST_COMPLETED)
        for fut in done:
            _settle_one(ctx, fut, inflight.pop(fut))
        _snapshot_workers(pool, seen)


def _effective_version(backend_cls: type[AnalyzerBackend]) -> str:
    """Producer version for skip checks, resolved before any worker spawn.

    ``LibrosaBackend.version`` is populated lazily inside ``analyze`` in the
    child; reading the class attribute in the parent would query for an empty
    version and re-analyze every item instead of skipping.
    """
    version = backend_cls.version
    if version:
        return version
    resolver = getattr(backend_cls, "_version", None)
    if callable(resolver):
        return str(resolver())
    return version


def _drain_pool(ctx: _RunContext, pool: ProcessPoolExecutor) -> None:
    """Pump the pool to empty, settling the batch's runner on every escape.

    ``seen_workers`` is why this is not one call: the stdlib exposes no
    public handle on the worker processes and ``shutdown`` drops the private
    one, so they are snapshotted while still reachable. Same reason as
    apps/analysis/pool.py -- a native crash (issue #1316's NULL instruction
    pointer, issue #1572's librosa SIGSEGV) produces no Python traceback at
    all, and the worker's exit SIGNAL is the only evidence of what happened.
    Without it a drain that hits one reports "A process in the process pool
    was terminated abruptly", which names neither the signal nor the cause.
    """
    conn, batch_id = ctx.conn, ctx.batch_id
    seen_workers: dict[int, object] = {}
    try:
        try:
            _pump_pool(ctx, pool, seen_workers)
        finally:
            _snapshot_workers(pool, seen_workers)
            pool.shutdown(wait=True, cancel_futures=True)
    except BackendUnavailable:
        queue_store.clear_batch_runner(conn, batch_id)
        raise
    except BrokenProcessPool as exc:
        queue_store.release_running_items(conn, batch_id)
        queue_store.clear_batch_runner(conn, batch_id)
        raise RuntimeError(
            pool_death_message(worker_exit_signals(seen_workers.values()))
        ) from exc
    except Exception:
        # Any other escape leaves this runner's claims uncommitted; hand them
        # back now instead of leaving them `running` until a takeover of this
        # exact batch, which a fresh enqueue never performs.
        queue_store.release_running_items(conn, batch_id)
        queue_store.clear_batch_runner(conn, batch_id)
        raise


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

    try:
        summary.released_on_takeover = queue_store.take_batch_runner(
            conn, batch_id, runner_id=rid, runner_pid=os.getpid()
        )
    except RuntimeError as exc:
        raise QueueError(str(exc)) from exc
    if batch.state == queue_store.BATCH_CANCELLED:
        raise QueueError(
            f"batch {batch_id!r} is cancelled; resume it before running it"
        )
    if batch.workers < 1:
        queue_store.set_batch_state(conn, batch_id, queue_store.BATCH_DONE)
        queue_store.clear_batch_runner(conn, batch_id)
        return summary

    version = _effective_version(backend_cls)
    backend_name = backend_cls.name
    owned_cache = ensure_owned_numba_cache_dir()
    warm_backend_jit(
        backend_cls,
        backend_name=backend_name,
        purge_stale=owned_cache is not None,
    )

    pool = spawn_pool(batch.workers)
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
    _drain_pool(ctx, pool)

    if summary.cancelled_midway:
        queue_store.clear_batch_runner(conn, batch_id)
        return summary
    counts = queue_store.counts_by_state(conn, batch_id)
    if counts[queue_store.ITEM_PENDING] == 0 and counts[queue_store.ITEM_RUNNING] == 0:
        queue_store.set_batch_state(conn, batch_id, queue_store.BATCH_DONE)
    queue_store.clear_batch_runner(conn, batch_id)
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


__all__ = [
    "BackendUnavailable",
    "RunSummary",
    "drain",
    "run_batch",
]
