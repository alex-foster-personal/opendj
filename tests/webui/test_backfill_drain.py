"""The daemon-side runner for one backfill batch (Re-analyze from the UI).

- [if] Re-analyze enqueue sends drain [then] queue_cli run starts for that batch off-thread, [else stop].

Every test here starts the REAL child (``queue_cli run`` through
``backfill_drain_job``) against a real state DB, running a SHIPPED producer
on copies of a tracked audio fixture (:mod:`tests.webui.drain_media`), and
most of them enqueue through the real ``/analysis/backfill/enqueue`` route.
The library rows behind those tracks come from the production folder import
of the copies; no test writes one.
No producer reports on itself: niceness, overlap and survival are read from
the process table while the children run, and outcomes from the queue rows
and the records the producer wrote. Where a property needs a host condition
(a missing capability, a thread that cannot start), that condition is set
on a separate real interpreter, so nothing in this process is patched.

Single-line intent, one assertion block each:
- if the drain runs anything but `queue_cli run` for the batch it was given,
  Re-analyze analyzes the wrong batch or nothing -- broken.
- if the child runs at normal priority, a Re-analyze competes with a playing
  deck -- broken.
- if a failed runner is swallowed, a batch sits queued with no cause in the
  engine log -- broken.
- if a runner that could not run (or could not even start, or whose thread
  could not start) leaves its items pending, the Re-analyze toast reads
  "queued" forever -- broken.
- if a cascade batch the dead runner created stays queued, it waits forever
  for a runner nobody will start -- broken.
- if settling turns a cancelled batch into "done", or fails a live runner's
  claims, the drain overrides a decision that was not its own -- broken.
- if two Re-analyze batches, or a Re-analyze batch and an ahead-analysis lane
  run, run their children at once, each sizes its pool as if it ran alone and
  together they exceed the memory ceiling admission budgets for -- broken.
- if the daemon shuts down mid-drain and the child or its pool worker keeps
  running, analysis outlives the app that started it -- broken.
- if a drain that starts after shutdown began still runs its child, or a
  stopped drain leaves its batch queued -- broken.
- if a finished drain stays registered, shutdown signals a dead pid (the
  overshoot) -- broken.
- if an ahead-analysis lane run that waited for the gate launches its child
  although a deck started playing (or the drain stopped) during the wait, the
  analysis lands on top of the set -- broken; if it never launches after a
  wait on an idle deck (the overshoot), the lane stalls behind every
  Re-analyze -- broken.
- if a drained batch an earlier engine left open is still queued after the
  next start, Re-analyze of that track is stuck behind it -- broken.
- if that start settles a planned batch, a terminal one, or one a drain in
  this process still owns (the overshoot) -- broken.
"""

from __future__ import annotations

import contextlib
import json
import locale
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from apps.analysis import queue as queue_api
from apps.analysis import queue_store, queue_targets
from apps.analysis.queue_cascade_note import cascade_note
from apps.analysis.store import fetch_records_by_ids, open_conn
from apps.webui.server import ahead_analysis, backfill_drain, backfill_drain_job
from apps.webui.server import app as app_mod
from apps.webui.server.routes import ingest_cli_procs
from tests.webui import drain_media
from tests.webui.drain_media import BACKEND, LANE, REPO, state_db

pytestmark = pytest.mark.requirement("NATIVE-5054")

JOIN_S: float = 180.0
#: Enough tracks that a drain is still analyzing when a test acts on it:
#: about 27 s on a 4-core host, and a test that stops it ends it in seconds.
LONG_BATCH: int = 200
#: What Re-analyze sends. Its capability is a Beat This! checkpoint, which
#: MDT_BEATGRID_WEIGHTS names; a child interpreter told a path with nothing
#: there has really lost it.
BEATGRID_LANE: str = "beatgrid"
BEATGRID_BACKEND: str = "own_beatgrid.backfill"
#: The priority every drain process must run at, read the way psutil reports it.
LOWEST: int = psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else backfill_drain_job.NICENESS


def _ids(prefix: str, count: int) -> list[str]:
    return [f"{prefix}_{n:03d}" for n in range(count)]


def _library(data_dir: Path, *groups: list[str]) -> tuple[Path, list[list[str]]]:
    """Import a fixture copy per name through the production folder import, once.

    Returns the state DB the import wrote and, per group of names, the
    stable_ids the import assigned them, in the group's order.
    """
    ids = drain_media.import_tracks(data_dir, [name for group in groups for name in group])
    return state_db(data_dir), [[ids[name] for name in group] for group in groups]


def _enqueue(
    db: Path,
    stable_ids: list[str],
    *,
    note: str = "test",
    lane: str = LANE,
    backend: str = BACKEND,
) -> str:
    """A batch over imported tracks, resolved and planned through the shipped queue API."""
    conn = open_conn(db)
    try:
        candidates = queue_targets.candidates_from_state(conn, stable_ids, lane=lane, backend=backend)
        result = queue_api.enqueue(conn, candidates, note=note)
    finally:
        conn.close()
    assert result.admitted == len(stable_ids)
    return result.batch_id


def _route(db: Path) -> TestClient:
    """The real app's routes over ``db``, without its lifespan."""
    app = app_mod.create_app()
    app.state.analysis_db_path = db
    return TestClient(app)


def _reanalyze(client: TestClient, stable_ids: list[str], **body: object) -> str:
    """POST the enqueue a Re-analyze sends (``drain: true``), for seeded tracks."""
    payload: dict[str, object] = {
        "stable_ids": stable_ids,
        "lane": LANE,
        "backend": BACKEND,
        "note": "track context menu",
        "drain": True,
        **body,
    }
    resp = client.post("/api/v1/analysis/backfill/enqueue", json=payload)
    assert resp.status_code == 201, resp.text
    planned = resp.json()
    assert planned["admitted"] == len(stable_ids), planned
    return planned["batch_id"]


def _drain_thread(batch_id: str) -> threading.Thread | None:
    return next(
        (t for t in threading.enumerate() if t.name == f"webui.backfill-drain.{batch_id}"),
        None,
    )


def _join(batch_id: str) -> None:
    thread = _drain_thread(batch_id)
    if thread is not None:
        thread.join(timeout=JOIN_S)
        assert not thread.is_alive(), f"the drain for {batch_id} did not finish in {JOIN_S:g} s"


@dataclass(frozen=True)
class _Seen:
    state: str
    items: list[tuple[str, str | None]]
    active_runner_pid: int | None


def _seen(db: Path, batch_id: str) -> _Seen:
    conn = open_conn(db)
    try:
        prog = queue_api.progress(conn, batch_id, item_limit=LONG_BATCH + 50)
        pid = conn.execute(
            "SELECT active_runner_pid FROM analysis_queue_batch WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    return _Seen(prog.state, [(item.state, item.reason) for item in prog.items], pid)


# --- the process table -----------------------------------------------------


def _runner_batch(cmdline: list[str]) -> str | None:
    """The batch a ``queue_cli run`` process runs (drain child or lane run), else None."""
    runner = "apps.webui.server.backfill_drain_job" in cmdline or "apps.analysis.queue_cli" in cmdline
    if not runner or "run" not in cmdline or "--batch-id" not in cmdline:
        return None
    return cmdline[cmdline.index("--batch-id") + 1]


@dataclass
class _Proc:
    cmdline: list[str]
    parent: int
    nices: list[int] = field(default_factory=list)


class _ProcessTable:
    """Samples every descendant of this test process until stopped.

    Each sample notes each live process's command line, parent and priority,
    and which batches had a runner process alive at that instant. A process
    that is exiting or has exited (a zombie included) is not live and is not
    counted.
    """

    INTERVAL_S: float = 0.01

    def __init__(self) -> None:
        self.procs: dict[int, _Proc] = {}
        self.runner_batches: set[str] = set()
        self.overlaps: list[set[str]] = []
        self.samples = 0
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="test.process-table", daemon=True)

    def __enter__(self) -> _ProcessTable:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        self.check()

    def check(self) -> None:
        """A sampler that died has measured nothing since; that is not a clean table."""
        assert self.error is None, f"the process table stopped sampling: {self.error!r}"
        assert self._thread.is_alive() or self._stop.is_set(), "the process table stopped sampling"

    def _loop(self) -> None:
        try:
            self._sample_until_stopped()
        except BaseException as exc:  # noqa: BLE001 -- surfaced by check()
            self.error = exc

    def _sample_until_stopped(self) -> None:
        me = psutil.Process()
        while not self._stop.is_set():
            running: set[str] = set()
            for proc in me.children(recursive=True):
                try:
                    with proc.oneshot():
                        cmdline, parent, nice = proc.cmdline(), proc.ppid(), proc.nice()
                except psutil.Error:
                    continue
                if not cmdline:
                    # Exiting: the kernel has already dropped its command line,
                    # a few ms before it shows as a zombie. It is not live.
                    continue
                seen = self.procs.setdefault(proc.pid, _Proc(cmdline, parent))
                # The latest command line wins: a process caught between fork
                # and exec first shows its parent's.
                seen.cmdline, seen.parent = cmdline, parent
                seen.nices.append(nice)
                batch = _runner_batch(cmdline)
                if batch is not None:
                    running.add(batch)
            if len(running) > 1:
                self.overlaps.append(running)
            self.runner_batches |= running
            self.samples += 1
            self._stop.wait(self.INTERVAL_S)

    def runner_pids(self, batch_id: str) -> list[int]:
        """The runner processes of ``batch_id``.

        A runner's own fork, caught between fork and exec as it starts a pool
        worker, still shows the runner's command line; it is not a runner.
        """
        named = {pid for pid, p in self.procs.items() if _runner_batch(p.cmdline) == batch_id}
        return [pid for pid in named if self.procs[pid].parent not in named]

    def descendants(self, root: int) -> list[int]:
        """Every sampled process whose ancestry reaches ``root``."""
        found = []
        for pid in self.procs:
            current = self.procs[pid].parent
            while current in self.procs and current != root:
                current = self.procs[current].parent
            if current == root:
                found.append(pid)
        return found

    def pool_workers(self, root: int) -> list[int]:
        return [
            pid
            for pid in self.descendants(root)
            if any("multiprocessing.spawn" in part for part in self.procs[pid].cmdline)
        ]

    def wait_for_pool_worker(self, batch_id: str) -> int:
        """A pool worker of ``batch_id``'s drain child, once one is seen alive."""
        deadline = time.monotonic() + JOIN_S
        while time.monotonic() < deadline:
            self.check()
            for runner in self.runner_pids(batch_id):
                workers = self.pool_workers(runner)
                if workers:
                    return workers[0]
            time.sleep(0.05)
        raise AssertionError(
            f"no pool worker of {batch_id} was seen within {JOIN_S:g} s; "
            f"{self.samples} samples, runners {self.runner_pids(batch_id)}, "
            f"{[(pid, p.parent, p.cmdline[:5]) for pid, p in list(self.procs.items())[:12]]}"
        )


def _gone(pid: int, within_s: float = 10.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


# --- a child interpreter, for host conditions this process must not take on ----


def _in_child(code: str, *args: object, **env: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a real interpreter at the repository root, configured by ``env``.

    Configuration crosses a real process boundary, the way the shipped app
    is configured, so nothing in this process is patched.
    """
    return subprocess.run(
        [sys.executable, "-c", code, *map(str, args)],
        env={**os.environ, **env},
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=JOIN_S,
        check=False,
    )


#: Re-analyze exactly as the track context menu sends it, through the real
#: route, in an interpreter whose beatgrid checkpoint is missing.
_REANALYZE_IN_CHILD = """
import sys, time
from pathlib import Path
from fastapi.testclient import TestClient
from apps.webui.server.app import create_app
db, stable_id = Path(sys.argv[1]), sys.argv[2]
app = create_app()
app.state.analysis_db_path = db
# The daemon answers loopback hosts only; this is the host the app itself uses.
client = TestClient(app, base_url="http://127.0.0.1")
resp = client.post("/api/v1/analysis/backfill/enqueue", json={
    "stable_ids": [stable_id], "lane": "beatgrid", "backend": "own_beatgrid.backfill",
    "note": "track context menu", "drain": True})
assert resp.status_code == 201, resp.text
batch_id = resp.json()["batch_id"]
deadline = time.monotonic() + 170
while time.monotonic() < deadline:
    prog = client.get("/api/v1/analysis/backfill/progress", params={"batch_id": batch_id}).json()
    if prog["state"] in ("done", "cancelled"):
        break
    time.sleep(0.1)
print(batch_id)
"""

#: One drain of an existing batch, run to its end in the child.
_DRAIN_IN_CHILD = """
import sys
from pathlib import Path
from apps.webui.server import backfill_drain
thread = backfill_drain.start(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
thread.join(170)
assert not thread.is_alive(), "the drain did not finish"
"""

#: A drain whose thread cannot be started: this interpreter asks for a thread
#: stack larger than any address space, so creating the thread really fails.
_START_WITHOUT_A_THREAD = """
import sys, threading
from pathlib import Path
from apps.webui.server import backfill_drain
db, batch_id = Path(sys.argv[1]), sys.argv[2]
threading.stack_size(1 << 60)
try:
    thread = backfill_drain.start(db, batch_id, "own_loudness.backfill")
finally:
    threading.stack_size(0)
print("thread" if thread is not None else "none", batch_id in backfill_drain._live)
"""


def _missing_checkpoint(tmp_path: Path) -> dict[str, str]:
    absent = tmp_path / "no-checkpoint-here" / "beat_this-final0.ckpt"
    assert not absent.exists()
    return {"MDT_BEATGRID_WEIGHTS": str(absent)}


# --- one real drain through the route --------------------------------------


@dataclass(frozen=True)
class _Drained:
    data_dir: Path
    db: Path
    batch_id: str
    #: The imported tracks' stable_ids, sorted, and the copy each was imported from.
    stable_ids: list[str]
    copies: dict[str, Path]
    returned_while_running: bool
    table: _ProcessTable


@pytest.fixture(scope="module")
def drained(tmp_path_factory: pytest.TempPathFactory) -> _Drained:
    """One real Re-analyze of imported tracks, through the route, watched from the process table."""
    data_dir = tmp_path_factory.mktemp("drained")
    names = _ids("ok_track", 8)
    imported = drain_media.import_tracks(data_dir, names)
    db = state_db(data_dir)
    copies = {sid: data_dir / "music" / f"{name}{drain_media.TRACK.suffix}" for name, sid in imported.items()}
    stable_ids = sorted(copies)
    with _ProcessTable() as table:
        batch_id = _reanalyze(_route(db), stable_ids)
        thread = _drain_thread(batch_id)
        returned_while_running = thread is not None and thread.is_alive()
        _join(batch_id)
    return _Drained(data_dir, db, batch_id, stable_ids, copies, returned_while_running, table)


def test_runner_argv_is_queue_cli_run_for_that_batch_through_the_priority_wrapper(
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.db"
    argv = backfill_drain.runner_argv(db, "qb_1", "own_beatgrid.backfill")
    expected = [
        sys.executable,
        "-m",
        "apps.webui.server.backfill_drain_job",
        "--db",
        str(db),
        "--json",
        "run",
        "--batch-id",
        "qb_1",
        "--backend",
        "own_beatgrid.backfill",
    ]
    assert argv[-len(expected) :] == expected
    if sys.platform != "darwin":
        assert argv == expected


def test_the_imported_tracks_resolve_to_their_fixture_copies(drained: _Drained) -> None:
    """The import made a live track of every copy, which the queue resolves to those bytes."""
    conn = open_conn(drained.db)
    try:
        candidates = queue_targets.candidates_from_state(conn, drained.stable_ids, lane=LANE, backend=BACKEND)
    finally:
        conn.close()
    assert len(set(drained.stable_ids)) == len(drained.copies) == 8
    for candidate in candidates:
        assert Path(candidate.file_path).resolve() == drained.copies[candidate.stable_id].resolve()
        assert candidate.duration_s == pytest.approx(drain_media.TRACK_DURATION_S, abs=0.1)
    seen = _seen(drained.db, drained.batch_id)
    assert len(seen.items) == len(drained.stable_ids), "the route did not enqueue every imported track"


@pytest.mark.requires_canonical_decode  # drains through own_loudness.backfill, which needs odj-audio decode
def test_start_drains_that_batch_for_real_off_the_calling_thread(drained: _Drained) -> None:
    assert drained.returned_while_running, "the route returned only after the child had finished"
    seen = _seen(drained.db, drained.batch_id)
    assert seen.state == "done"
    assert seen.items == [("done", None)] * len(drained.stable_ids)
    records = fetch_records_by_ids(drained.stable_ids, backend=BACKEND, db_path=drained.db)
    assert sorted(r.stable_id for r in records) == drained.stable_ids
    assert all(r.lanes[LANE].status == "ok" for r in records), "the producer measured nothing"
    assert drained.table.runner_batches == {drained.batch_id}


def test_the_child_and_its_pool_workers_run_at_the_lowest_priority(drained: _Drained) -> None:
    """Read from the live processes, not reported by them."""
    assert psutil.Process().nice() != LOWEST, (
        "this test process already runs at the lowest priority, so inheritance alone "
        "would pass; the priority cannot be measured here"
    )
    table = drained.table
    children = table.runner_pids(drained.batch_id)
    assert len(children) == 1, (children, {pid: (p.parent, p.cmdline[:4]) for pid, p in table.procs.items()})
    [child] = children
    assert table.procs[child].nices[-1] == LOWEST, table.procs[child].nices
    workers = table.pool_workers(child)
    assert workers, f"no pool worker of the drain child was seen in {table.samples} samples"
    for pid in table.descendants(child):
        proc = table.procs[pid]
        assert set(proc.nices) == {LOWEST}, (pid, proc.cmdline[:4], proc.nices)


def test_a_finished_drain_leaves_nothing_registered(drained: _Drained) -> None:
    ingest_cli_procs.reopen()
    assert ingest_cli_procs.stop_all() == 0, "a finished drain child stayed registered"


# --- runners that could not run --------------------------------------------


def test_a_runner_that_could_not_run_is_logged_and_fails_its_items_with_the_reason(
    tmp_path: Path,
) -> None:
    """Re-analyze as the menu sends it, on an engine whose beatgrid checkpoint is missing."""
    db, [[short]] = _library(tmp_path, ["short"])
    done = _in_child(_REANALYZE_IN_CHILD, db, short, **_missing_checkpoint(tmp_path))
    assert done.returncode == 0, done.stderr
    batch_id = done.stdout.strip()
    assert f"backfill drain for batch {batch_id}: runner exit 2" in done.stderr, done.stderr
    seen = _seen(db, batch_id)
    assert seen.state == "done"
    [(state, reason)] = seen.items
    assert state == "failed"
    assert reason is not None and reason.startswith("runner exit 2: "), reason
    assert "not available on this host" in reason
    assert "MDT_BEATGRID_WEIGHTS=" in reason, "the reason is not the producer's own"


def test_a_runner_that_cannot_even_start_settles_the_batch(tmp_path: Path) -> None:
    """A NUL byte in an argv element makes the launch itself raise, before any child exists."""
    db, [short] = _library(tmp_path, ["short"])
    batch_id = _enqueue(db, short)
    thread = backfill_drain.start(db, batch_id, f"{BACKEND}\0")
    assert thread is not None
    thread.join(timeout=JOIN_S)
    seen = _seen(db, batch_id)
    assert seen.state == "done"
    [(state, reason)] = seen.items
    assert state == "failed"
    assert reason is not None and reason.startswith("runner could not start: ValueError")


def test_a_drain_whose_thread_cannot_start_settles_the_batch_and_is_not_left_live(
    tmp_path: Path,
) -> None:
    """Codex P2 on PR #5054: a thread that fails to start must not strand its batch."""
    db, [short] = _library(tmp_path, ["short"])
    batch_id = _enqueue(db, short)
    done = _in_child(_START_WITHOUT_A_THREAD, db, batch_id)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["none", "False"], (
        "start() returned a thread, or left the batch marked live: " + done.stdout
    )
    assert f"backfill drain for batch {batch_id}: runner could not start: RuntimeError" in done.stderr
    seen = _seen(db, batch_id)
    assert seen.state == "done", "the batch stayed open with no thread to run it"
    [(state, reason)] = seen.items
    assert state == "failed"
    assert reason is not None and reason.startswith("runner could not start: RuntimeError"), reason


def test_a_cascade_batch_the_dead_runner_left_is_settled_with_it(tmp_path: Path) -> None:
    db, [parent, dependent] = _library(tmp_path, ["parent_track"], ["parent_track_key"])
    batch_id = _enqueue(db, parent, lane=BEATGRID_LANE, backend=BEATGRID_BACKEND)
    child_id = _enqueue(
        db,
        dependent,
        note=cascade_note("key", batch_id),
        lane="key",
        backend="own_key.backfill",
    )
    done = _in_child(_DRAIN_IN_CHILD, db, batch_id, BEATGRID_BACKEND, **_missing_checkpoint(tmp_path))
    assert done.returncode == 0, done.stderr
    [(parent_state, _)] = _seen(db, batch_id).items
    assert parent_state == "failed"
    seen = _seen(db, child_id)
    assert seen.state == "done"
    [(state, reason)] = seen.items
    assert state == "failed"
    assert reason is not None and "not available on this host" in reason


def test_a_cascade_batch_of_another_parent_is_left_alone(tmp_path: Path) -> None:
    """Control for the test above: only THIS drain's chain is settled."""
    db, [parent, other] = _library(tmp_path, ["parent_track"], ["other_track"])
    batch_id = _enqueue(db, parent, lane=BEATGRID_LANE, backend=BEATGRID_BACKEND)
    other_id = _enqueue(
        db,
        other,
        note=cascade_note("key", f"{batch_id}x"),
        lane="key",
        backend="own_key.backfill",
    )
    done = _in_child(_DRAIN_IN_CHILD, db, batch_id, BEATGRID_BACKEND, **_missing_checkpoint(tmp_path))
    assert done.returncode == 0, done.stderr
    [(parent_state, _)] = _seen(db, batch_id).items
    assert parent_state == "failed"
    seen = _seen(db, other_id)
    assert seen.state == "queued"
    assert seen.items == [("pending", None)]


def _latin1_runner(tmp_path: Path, *, checkpoint: bool = False) -> dict[str, str]:
    """A host whose analysis children write latin-1 (its PYTHONIOENCODING), and,
    unless ``checkpoint``, a beatgrid checkpoint path with an e-acute that is missing,
    so the runner's own error names a non-ASCII path."""
    assert locale.getpreferredencoding(False).lower().replace("-", "") == "utf8", (
        "this host decodes child output in a non-UTF-8 locale, so latin-1 output "
        "cannot fail to decode here; the case is not reproducible on this host"
    )
    env = {"PYTHONIOENCODING": "latin-1"}
    if not checkpoint:
        absent = tmp_path / "caf\u00e9-no-checkpoint" / "beat_this-final0.ckpt"
        assert not absent.exists()
        env["MDT_BEATGRID_WEIGHTS"] = str(absent)
    return env


def test_a_runner_whose_output_cannot_be_decoded_still_settles_its_batch(tmp_path: Path) -> None:
    """Codex P2 on PR #5054: the drain decodes the child's output, and a child
    that writes bytes that are not UTF-8 made that read raise past the settle."""
    db, [parent] = _library(tmp_path, ["parent_track"])
    batch_id = _enqueue(db, parent, lane=BEATGRID_LANE, backend=BEATGRID_BACKEND)
    done = _in_child(_DRAIN_IN_CHILD, db, batch_id, BEATGRID_BACKEND, **_latin1_runner(tmp_path))
    assert done.returncode == 0, done.stderr
    unread = f"backfill drain for batch {batch_id}: runner output could not be read (exit 2): UnicodeDecodeError"
    assert unread in done.stderr, done.stderr
    seen = _seen(db, batch_id)
    assert seen.state == "done", "the batch stayed open after its runner's output could not be read"
    [(state, reason)] = seen.items
    assert state == "failed"
    assert reason is not None and reason.startswith("runner output could not be read (exit 2): "), reason


@pytest.mark.requires_canonical_decode  # drains through own_loudness.backfill, which needs odj-audio decode
def test_a_latin1_runner_with_readable_output_settles_nothing(tmp_path: Path) -> None:
    """Control for the test above (the overshoot): the same host, a run whose output
    decodes, finishes its items with no unread-output error."""
    db, [ids] = _library(tmp_path, _ids("latin1", 2))
    batch_id = _enqueue(db, ids)
    done = _in_child(_DRAIN_IN_CHILD, db, batch_id, BACKEND, **_latin1_runner(tmp_path, checkpoint=True))
    assert done.returncode == 0, done.stderr
    assert "could not be read" not in done.stderr, done.stderr
    assert _seen(db, batch_id).items == [("done", None)] * len(ids)


# --- settling never overrides someone else's decision ----------------------


def _drain(db: Path, batch_id: str) -> None:
    thread = backfill_drain.start(db, batch_id, BACKEND)
    assert thread is not None and thread.daemon
    thread.join(timeout=JOIN_S)
    assert not thread.is_alive(), f"the drain for {batch_id} did not finish in {JOIN_S:g} s"


def test_a_cancelled_batch_stays_cancelled_when_its_runner_exits_nonzero(tmp_path: Path) -> None:
    db, [short] = _library(tmp_path, ["short"])
    batch_id = _enqueue(db, short)
    conn = open_conn(db)
    try:
        assert queue_api.cancel(conn, batch_id) == 1
    finally:
        conn.close()
    _drain(db, batch_id)
    seen = _seen(db, batch_id)
    assert seen.state == "cancelled"
    assert [state for state, _ in seen.items] == ["cancelled"]


def test_a_batch_a_live_runner_holds_is_left_to_that_runner(tmp_path: Path) -> None:
    """This test process takes the batch's lease through the runner's own API, as a
    second runner would; the drain's child is refused and must not fail its claims."""
    db, [short] = _library(tmp_path, ["short"])
    batch_id = _enqueue(db, short)
    conn = open_conn(db)
    try:
        queue_store.take_batch_runner(conn, batch_id, runner_id="other-runner", runner_pid=os.getpid())
    finally:
        conn.close()
    _drain(db, batch_id)
    seen = _seen(db, batch_id)
    assert seen.state == "running"
    assert seen.items == [("pending", None)]
    assert seen.active_runner_pid == os.getpid()


# --- one runner at a time ----------------------------------------------------


@pytest.mark.requires_canonical_decode  # drains through own_loudness.backfill, which needs odj-audio decode
def test_drains_started_back_to_back_run_one_at_a_time(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Three Re-analyze enqueues in a row; the process table never holds two runners."""
    db, groups = _library(tmp_path, *[_ids(name, 4) for name in ("first", "second", "third")])
    client = _route(db)
    with caplog.at_level(logging.ERROR, logger=backfill_drain.__name__), _ProcessTable() as table:
        batches = [_reanalyze(client, group) for group in groups]
        for batch_id in batches:
            _join(batch_id)
    assert caplog.records == [], caplog.text
    assert table.runner_batches == set(batches), "a drain's child was never seen running"
    assert table.overlaps == [], f"runner children overlapped: {table.overlaps[:3]}"
    for batch_id, group in zip(batches, groups, strict=True):
        assert _seen(db, batch_id).items == [("done", None)] * len(group)


@pytest.mark.requires_canonical_decode  # drains through own_loudness.backfill, which needs odj-audio decode
def test_an_ahead_analysis_lane_run_waits_for_a_running_reanalyze_drain(tmp_path: Path) -> None:
    """Both through their real launchers: the drain's child, then ahead-analysis's own
    ``queue_cli run`` child, started while the drain's child is still analyzing."""
    db, [reanalyze_ids, ahead_ids] = _library(tmp_path, _ids("reanalyze", 8), _ids("ahead", 2))
    ahead_batch = _enqueue(db, ahead_ids)
    with _ProcessTable() as table:
        drain_batch = _reanalyze(_route(db), reanalyze_ids)
        table.wait_for_pool_worker(drain_batch)
        code, _out, err = ahead_analysis.run_queued_batch(str(db), ahead_batch, BACKEND)
        _join(drain_batch)
    assert code == 0, err
    assert table.runner_batches == {drain_batch, ahead_batch}
    assert table.overlaps == [], f"the lane run ran during the drain: {table.overlaps[:3]}"
    assert _seen(db, drain_batch).items == [("done", None)] * len(reanalyze_ids)
    assert _seen(db, ahead_batch).items == [("done", None)] * len(ahead_ids)


#: Port pairs for apps built for ahead-analysis, distinct from the lifespan tests'.
_AHEAD_PORTS: dict[str, int] = {"playing": 18741, "stopped": 18742, "idle": 18743}

#: ahead-analysis's lane run exactly as ``build_for_app`` wires it, in an
#: interpreter whose data dir is the one the test imported into (the lane's
#: record reads use the app's own state DB, which MDT_DATA_DIR names). The
#: run starts while this interpreter holds the runner gate; once it has
#: enqueued its batch and waits, the deck mirror is PUT as the page sends it
#: (and the drain stopped, for "stopped"), then the gate is released.
_AHEAD_LANE_IN_CHILD = """
import json, sqlite3, sys, threading, time
from pathlib import Path
from fastapi.testclient import TestClient
from apps.webui.server import ahead_analysis, backfill_drain
from apps.webui.server.app import create_app
db, action, port, ids = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4:]
app = create_app(state_db_path=str(db), mount_frontend=False, port=port, frontend_port=port + 1000)
client = TestClient(app, base_url="http://127.0.0.1")
drain = ahead_analysis.build_for_app(app)
outcome = {}
def lane():
    try:
        outcome["errors"] = drain.sources.run_lane_fn("loudness", "own_loudness.backfill", ids)
    except BaseException as exc:
        outcome["raised"] = type(exc).__name__
def ahead_batch():
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT batch_id FROM analysis_queue_batch WHERE note = 'ahead-analysis'").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    return None if row is None else row[0]
with backfill_drain.RUNNER_GATE:
    thread = threading.Thread(target=lane, daemon=True)
    thread.start()
    deadline = time.monotonic() + 120
    while (batch_id := ahead_batch()) is None:
        assert thread.is_alive(), f"the lane run ended without waiting: {outcome}"
        assert time.monotonic() < deadline, "the lane run never enqueued its batch"
        time.sleep(0.02)
    assert not drain.sources.playing_fn(), "a deck was playing before the wait"
    resp = client.put("/api/v1/state/ui-mirror", json={"decks": {"1": {"playing": action == "playing"}}})
    assert resp.status_code == 202, resp.text
    if action == "stopped":
        drain.stop()
thread.join(170)
assert not thread.is_alive(), "the lane run did not finish after the gate opened"
print(json.dumps({"batch_id": batch_id, **outcome}))
"""


@dataclass
class _LaneRun:
    batch_id: str
    errors: dict[str, str] | None
    raised: str | None


def _lane_run_across_the_gate(tmp_path: Path, action: str) -> tuple[Path, list[str], _LaneRun, _ProcessTable]:
    db, [ids] = _library(tmp_path, _ids(action, 2))
    with _ProcessTable() as table:
        done = _in_child(_AHEAD_LANE_IN_CHILD, db, action, _AHEAD_PORTS[action], *ids, MDT_DATA_DIR=str(tmp_path))
    assert done.returncode == 0, done.stderr
    outcome = json.loads(done.stdout.strip().splitlines()[-1])
    assert any("enqueue" in p.cmdline for p in table.procs.values()), (
        "the table never saw the lane run's enqueue child, so it was not watching"
    )
    return db, ids, _LaneRun(outcome["batch_id"], outcome.get("errors"), outcome.get("raised")), table


def _yielded_without_a_child(db: Path, run: _LaneRun, table: _ProcessTable) -> None:
    assert run.raised == ahead_analysis.LaneRunYielded.__name__, (run.raised, run.errors)
    assert table.runner_batches == set(), f"a runner child launched: {table.runner_batches}"
    assert run.batch_id not in {_runner_batch(p.cmdline) for p in table.procs.values()}
    seen = _seen(db, run.batch_id)
    assert seen.state == "cancelled", "the yielded run left its batch for a runner that is not coming"
    assert [state for state, _ in seen.items] == ["cancelled", "cancelled"]


def test_an_ahead_lane_run_that_waited_launches_no_child_once_a_deck_is_playing(
    tmp_path: Path,
) -> None:
    """Codex P1 on PR #5054: the tick checked playback before the wait, not after it."""
    db, _, run, table = _lane_run_across_the_gate(tmp_path, "playing")
    _yielded_without_a_child(db, run, table)


def test_an_ahead_lane_run_that_waited_launches_no_child_once_its_drain_stopped(
    tmp_path: Path,
) -> None:
    db, _, run, table = _lane_run_across_the_gate(tmp_path, "stopped")
    _yielded_without_a_child(db, run, table)


@pytest.mark.requires_canonical_decode  # drains through own_loudness.backfill, which needs odj-audio decode
def test_an_ahead_lane_run_that_waited_on_an_idle_deck_runs_once_the_gate_opens(
    tmp_path: Path,
) -> None:
    """Control for the two above (the overshoot): waiting alone is no reason to yield."""
    db, ids, run, table = _lane_run_across_the_gate(tmp_path, "idle")
    assert run.raised is None, run.raised
    assert run.errors == {}, run.errors
    assert table.runner_batches == {run.batch_id}, "the lane run never launched its child"
    seen = _seen(db, run.batch_id)
    assert seen.items == [("done", None)] * len(ids)
    records = fetch_records_by_ids(ids, backend=BACKEND, db_path=db)
    assert sorted(r.stable_id for r in records) == sorted(ids)


# --- no drain child outlives the daemon --------------------------------------


def _app(db: Path, port: int) -> object:
    return app_mod.create_app(state_db_path=str(db), mount_frontend=False, port=port, frontend_port=port + 1000)


def _stopped_mid_run(seen: _Seen) -> None:
    """Some items analyzed or not, none left open, the rest failed with the stop."""
    states = [state for state, _ in seen.items]
    assert seen.state == "done", seen.state
    assert "pending" not in states and "running" not in states, "an item was left open"
    stopped = [reason for state, reason in seen.items if state == "failed"]
    assert stopped, "every item finished: the drain was not stopped mid-run"
    assert all(r is not None and r.startswith("runner exit ") for r in stopped), stopped[:3]


def test_the_daemon_shutdown_stops_a_running_drain_child_and_its_pool(tmp_path: Path) -> None:
    """The REAL lifespan of a real app: its shutdown, not a direct call, stops the child."""
    ingest_cli_procs.reopen()
    db, [stable_ids] = _library(tmp_path, _ids("held", LONG_BATCH))
    worker = child = None
    try:
        with _ProcessTable() as table:
            with TestClient(_app(db, 18737)) as client:
                batch_id = _reanalyze(client, stable_ids)
                worker = table.wait_for_pool_worker(batch_id)
                [child] = table.runner_pids(batch_id)
                thread = _drain_thread(batch_id)
                assert thread is not None and thread.is_alive(), "the drain ended before shutdown"
            thread.join(timeout=JOIN_S)
        assert not thread.is_alive(), "shutdown left the drain waiting on its child"
        assert _gone(child), "shutdown left the drain's child running"
        assert _gone(worker), "shutdown left the drain's pool worker running"
        _stopped_mid_run(_seen(db, batch_id))
    finally:
        for pid in (worker, child):
            if pid is not None:
                with contextlib.suppress(psutil.NoSuchProcess):
                    psutil.Process(pid).kill()


def test_a_drain_that_starts_after_shutdown_began_never_runs_its_child(tmp_path: Path) -> None:
    """A drain still waiting for the gate at shutdown reaches its launch afterwards."""
    db, [stable_ids] = _library(tmp_path, _ids("late", 4))
    client = _route(db)
    assert ingest_cli_procs.stop_all() == 0
    try:
        batch_id = _reanalyze(client, stable_ids)
        _join(batch_id)
    finally:
        ingest_cli_procs.reopen()
    seen = _seen(db, batch_id)
    assert seen.state == "done"
    assert [state for state, _ in seen.items] == ["failed"] * len(stable_ids), "the late child analyzed a track"
    assert all(r is not None and r.startswith("runner exit ") for _, r in seen.items), seen.items


# --- the next start settles what an earlier engine left ----------------------


#: A cascade batch is for the dependent lane, planned for its own producer.
_KEY: dict[str, str] = {"lane": "key", "backend": "own_key.backfill"}


def _batch_row(db: Path, batch_id: str) -> tuple[str, str]:
    conn = open_conn(db)
    try:
        return conn.execute(
            "SELECT state, updated_at FROM analysis_queue_batch WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    finally:
        conn.close()


def _cancel(db: Path, batch_id: str) -> None:
    conn = open_conn(db)
    try:
        assert queue_api.cancel(conn, batch_id) == 1
    finally:
        conn.close()


def test_the_next_start_settles_a_drained_batch_an_earlier_engine_left_open(
    tmp_path: Path,
) -> None:
    """Seeded as the route leaves a drained batch and its cascade when the engine dies
    mid-chain; the REAL lifespan of a fresh app settles them before its first request."""
    ingest_cli_procs.reopen()
    db, tracks = _library(
        tmp_path,
        *[
            [name]
            for name in (
                "orphan_track",
                "orphan_cascade",
                "orphan_cascade_done",
                "planned_track",
                "finished_track",
                "finished_cascade",
            )
        ],
    )
    orphan_t, cascade_open_t, cascade_done_t, planned_t, finished_t, finished_cascade_t = tracks
    orphan = _enqueue(db, orphan_t, note=backfill_drain.drain_note("track context menu"))
    cascade_open = _enqueue(db, cascade_open_t, note=cascade_note("key", orphan), **_KEY)
    cascade_done = _enqueue(db, cascade_done_t, note=cascade_note("key", orphan), **_KEY)
    _cancel(db, cascade_done)
    planned = _enqueue(db, planned_t, note="planned from the panel")
    finished = _enqueue(db, finished_t, note=backfill_drain.drain_note(None))
    # A cascade its (now terminal) drained parent created and never ran.
    finished_cascade = _enqueue(db, finished_cascade_t, note=cascade_note("key", finished), **_KEY)
    _cancel(db, finished)
    untouched = {batch_id: _batch_row(db, batch_id) for batch_id in (cascade_done, planned, finished)}

    with TestClient(_app(db, 18738)) as client:
        # Read before the first request: startup has already run, nothing else has.
        for batch_id in (orphan, cascade_open, finished_cascade):
            seen = _seen(db, batch_id)
            assert seen.state == "done", f"{batch_id} is still {seen.state} after startup"
            assert seen.items == [("failed", backfill_drain.INTERRUPTED_REASON)]
        assert client.get("/api/v1/health").status_code == 200
    for batch_id, row in untouched.items():
        assert _batch_row(db, batch_id) == row, f"startup rewrote {batch_id}"
    assert _seen(db, planned).items == [("pending", None)]


@pytest.mark.requires_canonical_decode  # drains through own_loudness.backfill, which needs odj-audio decode
def test_the_next_start_leaves_drains_live_in_this_process_alone(tmp_path: Path) -> None:
    """Drains still alive when another app in this process starts belong to those drains.

    Two of them: one whose child is running (its batch is held by a live runner
    pid), and one queued behind it on the gate, whose batch nothing in the
    database marks as taken. Only the second proves the in-process guard.
    """
    ingest_cli_procs.reopen()
    db, [running_ids, waiting_ids] = _library(tmp_path, _ids("live", LONG_BATCH), _ids("waiting", 2))
    client = _route(db)
    worker = None
    try:
        with _ProcessTable() as table:
            running = _reanalyze(client, running_ids)
            worker = table.wait_for_pool_worker(running)
            waiting = _reanalyze(client, waiting_ids)
            assert backfill_drain.settle_orphaned_drains(db) == []
            with TestClient(_app(db, 18739)):
                seen = _seen(db, running)
                assert seen.state == "running", "startup settled a running drain"
                assert backfill_drain.INTERRUPTED_REASON not in {r for _, r in seen.items}
                seen = _seen(db, waiting)
                assert (seen.state, seen.items) == ("queued", [("pending", None)] * 2), (
                    "startup settled a drain waiting on the gate in this process"
                )
            # That app's shutdown stopped the running child. The waiting drain was
            # started before that app's lifespan, so when it gets the gate it
            # launches nothing and settles its own batch, with the reason the
            # startup would have used had it not been alive.
            _join(running)
            _join(waiting)
        assert running in table.runner_batches
        assert waiting not in table.runner_batches, "a drain from an earlier lifespan launched its child"
        _stopped_mid_run(_seen(db, running))
        seen = _seen(db, waiting)
        assert seen.items == [("failed", backfill_drain.INTERRUPTED_REASON)] * 2, seen.items
    finally:
        ingest_cli_procs.reopen()
        if worker is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(worker).kill()


@pytest.mark.requires_canonical_decode  # drains through own_loudness.backfill, which needs odj-audio decode
def test_a_drain_waiting_through_a_lifespan_restart_launches_nothing_in_the_next(
    tmp_path: Path,
) -> None:
    """Two REAL lifespans in one process (PR #5054 review round 7).

    A drain started under the first waits on the gate through that lifespan's
    shutdown, which cannot stop it (it has no child yet), and through the
    second's start, which leaves it alone because it is alive. It gets the gate
    only then: it must launch no child and settle its batch. A drain the second
    lifespan started gets the same gate and runs (the control for the overshoot).
    """
    ingest_cli_procs.reopen()
    db, [before_ids, after_ids] = _library(tmp_path, _ids("before", 2), _ids("after", 2))
    held = backfill_drain.RUNNER_GATE.acquire(timeout=JOIN_S)
    assert held, "another runner kept the gate"
    try:
        with _ProcessTable() as table:
            with TestClient(_app(db, 18744)) as first:
                before = _reanalyze(first, before_ids)
            thread = _drain_thread(before)
            assert thread is not None and thread.is_alive(), "the waiting drain did not outlive its lifespan"
            with TestClient(_app(db, 18745)) as second:
                assert _seen(db, before).items == [("pending", None)] * 2, "startup settled a live drain"
                after = _reanalyze(second, after_ids)
                backfill_drain.RUNNER_GATE.release()
                held = False
                _join(before)
                _join(after)
    finally:
        if held:
            backfill_drain.RUNNER_GATE.release()
        ingest_cli_procs.reopen()
    assert before not in table.runner_batches, "the drain from the first lifespan launched its child"
    seen = _seen(db, before)
    assert (seen.state, seen.items) == ("done", [("failed", backfill_drain.INTERRUPTED_REASON)] * 2), seen
    assert after in table.runner_batches, "the second lifespan's drain never ran its child"
    assert _seen(db, after).items == [("done", None)] * 2
