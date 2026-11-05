"""Run one backfill batch from the daemon, in a child process.

The track context menu's Re-analyze enqueues a batch over HTTP. Until this
module existed nothing in the app ever ran it: the only runner was
``python -m apps.analysis.queue_cli run`` typed by hand, which a packaged app
has no terminal for, so the batch sat at "queued, 0 done" forever.

The drain still never runs inside a request worker. :func:`start` returns at
once; a daemon thread waits on a child process running the same
``queue_cli run`` the CLI operator runs, through
:mod:`.backfill_drain_job`, which drops the child to the lowest scheduling
priority itself so it does not compete with a playing deck.

A drain child does not outlive the daemon. It is registered with
:mod:`.routes.ingest_cli_procs` while it runs, so the lifespan shutdown that
stops a refresh job's CLIs stops it and its pool workers too, and the batch
is settled with the stopped child's exit code. A drain still waiting its
turn when shutdown begins has its child killed the moment it starts. A drain
that gets its turn only after another app's lifespan reopened the registry in
the same process launches nothing: it started in an earlier registry epoch
(:func:`.routes.ingest_cli_procs.epoch`), so it settles its batch `failed`
with :data:`INTERRUPTED_REASON`, which the next start would have used had it
not still been alive.

A drain the engine could not finish is settled on the next start. The
route marks every batch it will drain in its note (:func:`drain_note`),
in the same insert that creates it, so a batch whose drain thread never
got its turn, or never got to settle it, is recognizable after the
process is gone. :func:`settle_orphaned_drains` runs in the app's lifespan
before it serves a request, and settles each such batch and its cascade
`failed` with :data:`INTERRUPTED_REASON`, leaving any batch a drain in this
process still owns alone.

Drains are serialized. Admission sizes each batch's process pool (up to four
workers) under a memory model that assumes it is the ONLY batch running, so
two children at once would double the ceiling it budgets for. Each drain's
thread therefore holds :data:`RUNNER_GATE` while its child runs, and a drain
started while another runs waits for it, its batch still ``queued``. The
ahead-of-time drain (:mod:`.ahead_analysis`) holds the same gate around its
own ``queue_cli run``, so a Re-analyze child and an ahead-analysis lane child
never overlap either. Waiting drains are not ordered among themselves.

Requirements (mini-PRD):
  ✔︎ ✅ start(): run exactly this batch, off the calling thread.
    [if] the argv is anything but `queue_cli run` for this batch [then ✖︎]
    [if] the calling thread blocks until the run ends [then ✖︎]
    [if] the child is started with a `preexec_fn` from this thread [then ✖︎]
  ✔︎ ✅ a failed runner names itself in the engine log.
    [if] the child exits nonzero and nothing is logged [then ✖︎]
    [if] the child cannot even be started and nothing is logged [then ✖︎]
  ✔︎ ✅ a runner that could not run settles the batch, and the cascade
     batches it created.
    [if] the child exits nonzero and an item stays pending [then ✖︎]
    [if] the child dies inside a dependency cascade and the cascade batch
         stays queued [then ✖︎]
    [if] the child cannot be started and the batch stays queued [then ✖︎]
    [if] the child's output cannot be read (not UTF-8) and the batch stays
         queued [then ✖︎]
    [if] the drain's thread cannot be started and the batch stays queued,
         or stays marked live [then ✖︎]
  ✔︎ ✅ settling never overrides someone else's decision.
    [if] a cancelled batch is settled as done [then ✖︎]
    [if] a batch a live runner holds has its items failed [then ✖︎]
  ✔︎ ✅ one analysis runner at a time in this daemon (PR #5054 review).
    [if] two Re-analyze batches run their children at once [then ✖︎]
    [if] a Re-analyze child and an ahead-analysis lane run overlap [then ✖︎]
    [if] start() waits for an earlier drain before returning [then ✖︎]
  ✔︎ ✅ no drain child outlives the daemon (PR #5054 review).
    [if] the daemon shuts down mid-drain and the child or a pool worker
         keeps running [then ✖︎]
    [if] a drain stopped by shutdown leaves its batch queued [then ✖︎]
    [if] a drain that starts after shutdown began runs its child [then ✖︎]
    [if] a drain still waiting at shutdown runs its child under the next
         lifespan in the same process, or leaves its batch queued [then ✖︎]
    [if] a drain started in the current lifespan is retired [then ✖︎]
  ✔︎ ✅ a drain an earlier engine never finished is settled on the next
     start (PR #5054 review).
    [if] a drained batch is left queued or running after a restart [then ✖︎]
    [if] a batch enqueued without drain is settled [then ✖︎]
    [if] a terminal batch is rewritten [then ✖︎]
    [if] a batch a live drain in this process owns is settled [then ✖︎]
  ⛔ retrying and timeouts.

-Claude
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from apps.analysis import queue_store
from apps.analysis import store as analysis_store
from apps.analysis.queue_cascade_note import (
    DRAIN_NOTE_MARK,
    cascade_children,
    cascade_parent,
    is_drain_note,
)
from apps.shared.process_priority import background_argv
from apps.shared.state import db as state_db
from apps.webui.server.routes import ingest_cli_procs

log = logging.getLogger(__name__)

SERVICE_ID: str = "com.opendj.engine.backfill-drain"
REASON_CHARS: int = 600
# The mark ``apps.analysis.queue_cli`` puts on the error it prints to stdout.
_CLI_ERROR_MARK = "[ERROR]"
#: A drain is one batch plus the follow-ups its cascade created; the runner
#: refuses more than 8 (``queue_runner.drain``), so this bound is never the
#: one that stops a real chain.
MAX_CHAIN: int = 32

#: Held by every analysis runner child this daemon starts, for as long as the
#: child runs: a Re-analyze drain here, an ahead-analysis lane run in
#: :mod:`.ahead_analysis`. Each child sizes its pool as if it ran alone.
RUNNER_GATE: threading.Lock = threading.Lock()

#: The reason a batch an earlier engine left unfinished is settled with.
INTERRUPTED_REASON: str = (
    "interrupted: the engine stopped before this Re-analyze batch finished; Re-analyze the track again"
)

#: Batches whose drain thread is alive in this process, chain roots only.
_live_lock = threading.Lock()
_live: set[str] = set()


def drain_note(note: str | None) -> str:
    """The note of a batch the engine will drain: the caller's note, marked."""
    return f"{note} {DRAIN_NOTE_MARK}" if note else DRAIN_NOTE_MARK


def runner_argv(db: Path, batch_id: str, backend: str) -> list[str]:
    """The child's argv: ``queue_cli run`` for this batch, at background priority."""
    return background_argv(
        [
            sys.executable,
            "-m",
            "apps.webui.server.backfill_drain_job",
            "--db",
            str(db),
            "--json",
            "run",
            "--batch-id",
            batch_id,
            "--backend",
            backend,
        ]
    )


def _run(db: Path, batch_id: str, backend: str, started_in: int | None = None) -> None:
    try:
        proc = subprocess.Popen(
            runner_argv(db, batch_id, backend),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "AF_SERVICE_ID": SERVICE_ID},
        )
    except Exception as exc:  # noqa: BLE001 -- any launch failure must still settle the batch
        reason = f"runner could not start: {type(exc).__name__}: {exc}"[:REASON_CHARS]
        log.error("backfill drain for batch %s: %s", batch_id, reason)
        _settle_or_log(db, batch_id, reason)
        return
    # Registered with the engine's child registry, so its lifespan shutdown
    # stops this child and its pool workers like a refresh step's CLI; one
    # started after shutdown began is killed as it registers.
    ingest_cli_procs.register(proc, started_in=started_in)
    try:
        stdout, stderr = proc.communicate()
    except Exception as exc:  # noqa: BLE001 -- unread output must still settle the batch
        # Output that cannot be decoded (a runner writing in another encoding)
        # raises here after the child exited; anything else may leave it running.
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reason = (f"runner output could not be read (exit {proc.returncode}): {type(exc).__name__}: {exc}")[
            :REASON_CHARS
        ]
        log.error("backfill drain for batch %s: %s", batch_id, reason)
        _settle_or_log(db, batch_id, reason)
        return
    finally:
        ingest_cli_procs.unregister(proc)
    if proc.returncode != 0:
        output = " ".join(_failure_text(stdout, stderr).split())[:REASON_CHARS]
        reason = f"runner exit {proc.returncode}: {output}"
        log.error("backfill drain for batch %s: %s", batch_id, reason)
        _settle_or_log(db, batch_id, reason)


def _failure_text(stdout: str | None, stderr: str | None) -> str:
    """The part of a failed runner's output that says why it failed.

    The CLI prints a host or batch error to stdout after the ``[ERROR]`` mark,
    while stderr carries its INFO progress lines (worker sizing, pool startup)
    as well as any traceback. A marked error wins, so progress logged before
    it cannot crowd it out of the reason; otherwise stderr, then stdout.
    """
    out = stdout or ""
    mark = out.find(_CLI_ERROR_MARK)
    if mark >= 0:
        return out[mark:]
    return stderr or out or "no output"


def _settle_or_log(db: Path, batch_id: str, reason: str) -> None:
    try:
        _fail_unrun_items(db, batch_id, reason)
    except Exception:
        log.exception("backfill drain for batch %s: could not settle the batch", batch_id)


def _fail_unrun_items(db: Path, batch_id: str, reason: str) -> None:
    """Settle what the runner left open, so the UI reaches a terminal state.

    The runner returns items to ``pending`` when the host cannot run the
    producer, for an operator who will fix the host and run again. A batch
    started from the UI has no such operator: this was its only runner. The
    same holds for every dependency-cascade batch that runner created and
    did not get to, so the whole chain is settled, not only the batch the
    UI is watching.
    """
    conn = analysis_store.open_conn(db)
    try:
        for chained_id in _chain(conn, batch_id):
            _settle_batch(conn, chained_id, reason)
    finally:
        conn.close()


def _chain(conn: sqlite3.Connection, batch_id: str) -> list[str]:
    """``batch_id`` and every cascade batch descended from it, parents first."""
    chain: list[str] = []
    frontier = [batch_id]
    while frontier and len(chain) < MAX_CHAIN:
        current = frontier.pop(0)
        if current in chain:
            continue
        chain.append(current)
        frontier.extend(cascade_children(conn, current))
    return chain


def _settle_batch(conn: sqlite3.Connection, batch_id: str, reason: str) -> bool:
    """Fail one batch's open items, unless it is no longer this drain's to settle.

    Two decisions are someone else's and survive it: a batch a live runner
    holds is left alone, because its ``running`` items are that runner's
    claims; and a batch the user cancelled ends ``cancelled``, never
    ``done``, because its cancelled items are not open and ``resume`` puts
    them back. Everything runs under one ``BEGIN IMMEDIATE``, so a runner
    cannot take the batch between the check and the writes. Returns whether
    it settled the batch.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT state, active_runner_pid FROM analysis_queue_batch WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        held = row is not None and row[1] is not None and queue_store.pid_is_alive(int(row[1]))
        if row is None or held:
            conn.execute("ROLLBACK")
            return False
        open_items = queue_store.list_items(conn, batch_id, states=(queue_store.ITEM_PENDING, queue_store.ITEM_RUNNING))
        for item in open_items:
            queue_store.finish_item(
                conn,
                batch_id=batch_id,
                stable_id=item.stable_id,
                lane=item.lane,
                state=queue_store.ITEM_FAILED,
                reason=reason,
            )
        # Read the items, not the batch row: a runner that refused a cancelled
        # batch had already flipped it to running when it took the lease.
        cancelled = queue_store.counts_by_state(conn, batch_id)[queue_store.ITEM_CANCELLED]
        queue_store.set_batch_state(
            conn, batch_id, queue_store.BATCH_CANCELLED if cancelled else queue_store.BATCH_DONE
        )
        queue_store.clear_batch_runner(conn, batch_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


def _run_gated(db: Path, batch_id: str, backend: str, started_in: int) -> None:
    """Run the child once no other analysis runner in this daemon is running.

    Only in the registry epoch the drain was started in: a lifespan that ended
    while this thread waited could not stop it (it had no child yet), and the
    next one must not inherit it.
    """
    try:
        with RUNNER_GATE:
            if ingest_cli_procs.epoch() != started_in:
                log.warning(
                    "backfill drain for batch %s: its engine stopped while it waited; launching nothing",
                    batch_id,
                )
                _settle_or_log(db, batch_id, INTERRUPTED_REASON)
                return
            _run(db, batch_id, backend, started_in)
    finally:
        with _live_lock:
            _live.discard(batch_id)


def settle_orphaned_drains(db: Path) -> list[str]:
    """Settle every drained batch an earlier engine left open; return their ids.

    Only batches the route marked for draining, and the cascade batches
    descended from one, are touched: a batch planned from the Backfill
    Queue panel or the CLI waits for its operator. Terminal batches are
    left as they are, a batch a live runner process holds is left to it
    (:func:`_settle_batch`), and so is every chain whose drain is alive in
    this process.

    The search runs on a read-only connection, so a start with nothing to
    settle writes nothing: opening the analysis store writably migrates its
    schema, which would move that work (and its lock) from the first route
    that needs it into every engine start.
    """
    if not db.exists():
        return []
    with _live_lock:
        live = set(_live)
    ro = state_db.open_ro(db)
    try:
        if not _has_batch_table(ro):
            return []
        roots: list[str] = []
        for batch_id, note in ro.execute(
            "SELECT batch_id, note FROM analysis_queue_batch WHERE state IN (?, ?) ORDER BY created_at, batch_id",
            (queue_store.BATCH_QUEUED, queue_store.BATCH_RUNNING),
        ).fetchall():
            root = _drain_root(ro, batch_id, note)
            if root is not None and root not in live and root not in roots:
                roots.append(root)
    finally:
        ro.close()
    if not roots:
        return []
    settled: list[str] = []
    conn = analysis_store.open_conn(db)
    try:
        for root in roots:
            # Settling is the side effect; a batch is listed only if it was settled.
            settled.extend(
                chained_id
                for chained_id in _chain(conn, root)
                if _is_open(conn, chained_id) and _settle_batch(conn, chained_id, INTERRUPTED_REASON)
            )
    finally:
        conn.close()
    return settled


def _has_batch_table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analysis_queue_batch'").fetchone()
        is not None
    )


def _is_open(conn: sqlite3.Connection, batch_id: str) -> bool:
    row = conn.execute("SELECT state FROM analysis_queue_batch WHERE batch_id = ?", (batch_id,)).fetchone()
    return row is not None and row[0] in (queue_store.BATCH_QUEUED, queue_store.BATCH_RUNNING)


def _drain_root(conn: sqlite3.Connection, batch_id: str, note: str | None) -> str | None:
    """The drained batch ``batch_id`` descends from (itself included), else None."""
    current, current_note = batch_id, note
    for _ in range(MAX_CHAIN):
        if is_drain_note(current_note):
            return current
        parent = cascade_parent(current_note)
        if parent is None:
            return None
        row = conn.execute("SELECT note FROM analysis_queue_batch WHERE batch_id = ?", (parent,)).fetchone()
        if row is None:
            return None
        current, current_note = parent, row[0]
    return None


def start(db: Path, batch_id: str, backend: str) -> threading.Thread | None:
    """Start the runner for ``batch_id`` and return its (daemon) thread.

    Returns at once. The thread runs the child only while it holds
    :data:`RUNNER_GATE`, so it first waits out any other analysis runner.

    Returns None when the thread itself could not be started (the host is
    out of threads or memory for a stack): nothing will ever run the batch,
    so it is settled here with the reason, exactly as :func:`_run` settles
    a child that could not be launched, and it is not left marked live.
    """
    started_in = ingest_cli_procs.epoch()
    with _live_lock:
        _live.add(batch_id)
    try:
        thread = threading.Thread(
            target=_run_gated,
            args=(db, batch_id, backend, started_in),
            name=f"webui.backfill-drain.{batch_id}",
            daemon=True,
        )
        thread.start()
    except Exception as exc:  # noqa: BLE001 -- any start failure must still settle the batch
        with _live_lock:
            _live.discard(batch_id)
        reason = f"runner could not start: {type(exc).__name__}: {exc}"[:REASON_CHARS]
        log.error("backfill drain for batch %s: %s", batch_id, reason)
        _settle_or_log(db, batch_id, reason)
        return None
    return thread
