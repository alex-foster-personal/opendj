"""Auto-drain the rekordbox-unmapped analysis backlog: analyze on import.

A track can land in ``state.db`` from several places, and only one of them is
this daemon: the folder-ingest CLI, the first-run setup wizard, a generated
e2e fixture library and the stems writer all write tracks from their own
process. An in-process event bus cannot reach across that boundary, so this
watcher RECONCILES instead of subscribing: it re-reads
:func:`apps.analysis.backlog.scan` on a timer and starts the drain whenever
the queue is non-empty. Any import path, no enqueue call to forget.

Two properties keep a reconcile loop from becoming a nuisance:

* It never runs two drains. A tick while the shared refresh job is running is
  a no-op, so the manual "Refresh analysis" button and the auto-drain cannot
  fight over the one job slot.
* It never retries an unchanged failed queue. Each attempt records the
  backlog signature it fired on; a tick whose signature matches is a no-op.
  A drain that fails (no analysis extra installed, an undecodable file) stays
  visibly failed in ``GET /ingest/refresh/status`` instead of relaunching a
  subprocess every interval. A NEW import changes the signature and re-arms
  it, which is exactly the trigger this module exists for.

A PARTIAL drain (some tracks analyzed, some failed) shrinks the queue, so the
signature changes and one more attempt fires over the survivors. That attempt
terminates the retry: if it fails again the signature is now unchanged.
Progress always re-arms the loop; standing still never does.

``MUSIC_DJ_AUTO_ANALYZE`` is a fail-fast enum, ``on`` or ``off``, defaulting
to ``on``. Anything else raises rather than being coerced to a guess. Only
the real daemon reads it (``app.py:_build_default_app``); ``create_app`` takes
``auto_analyze=False`` so no test builds a thread that shells out.

Requirements (mini-PRD):
  ✔︎ ✅ tick(): one reconcile step, with its outcome named.
    [if] the watcher is disabled [then] tick reports "disabled" and starts nothing
    [if] a refresh job is already running [then] tick reports "busy"
    [if] the backlog is empty [then] tick reports "empty" and forgets the
      recorded signature, so a queue that empties and returns identical is
      still drained
    [if] the pending signature matches the last attempt [then] tick reports
      "unchanged" and starts nothing
    [if] a new track lands unmapped [then] tick reports "started" and the
      attempt is booked, on the next tick, against the queue the WORKER read
    [if] starting the drain raises [then] no attempt is recorded, so the next
      tick over the same queue retries instead of reporting "unchanged"
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from apps.analysis import backends
from apps.analysis.backlog import Backlog

log = logging.getLogger(__name__)

AUTO_ANALYZE_ENV: str = "MUSIC_DJ_AUTO_ANALYZE"
AUTO_ANALYZE_VALUES: tuple[str, ...] = ("on", "off")
DEFAULT_INTERVAL_S: float = 60.0

#: Name of the reconcile thread. A test asserts the daemon really started
#: and really stopped one by looking for it in ``threading.enumerate()``.
THREAD_NAME: str = "webui.auto-analyze"

#: How long a restarting :meth:`AutoAnalyzeWatcher.start` waits for a loop
#: that outlived its own ``stop()``. Bounded because this runs on the daemon's
#: startup path: the usual survivor is finishing a slow scan and returns
#: almost at once, and a genuinely stuck one must cost startup this much and
#: no more.
RESTART_JOIN_S: float = 5.0

ScanFn = Callable[[], Backlog]
RunningFn = Callable[[], bool]
#: Reads the queue snapshot ONE drain's worker actually read, or None if that
#: drain published none. Never the watcher's own pre-start scan.
ConsumedFn = Callable[[], str | None]
@dataclass(frozen=True)
class Attempt:
    """The newest finished drain: an opaque identity plus what its worker read.

    ``job`` is compared by identity and NEVER dereferenced here, so the
    watcher stays ignorant of the refresh-job type. The watcher keeps the
    reference it was handed, which is what makes the identity safe to compare:
    the object cannot be freed and have its id recycled by a later job while
    something still points at it.

    The identity is the point. Without it the watcher cannot tell "a drain
    finished since my last tick" from "the same finished drain is still
    sitting in the one-slot registry", and re-reading the latter would undo
    the deliberate forget an empty queue performs.

    ``targets_backlog`` is what makes ``signature is None`` READABLE. Two very
    different drains publish nothing: one that never targeted this backlog at
    all (a library or batch sweep), and one that targeted it and is saying so
    - the CLI reported a target it never admitted, so the worker cleared the
    signature precisely to stop this queue being booked. Only the caller knows
    which, so it says, and the watcher can then treat a cleared signature as
    the statement it is instead of skipping it as a silence.
    """

    job: object
    signature: str | None
    targets_backlog: bool = False


class DrainRefused(RuntimeError):
    """A start refused by the guard the watcher hands to :data:`StartFn`.

    Raised from inside the registry's own lock, which is the whole point:
    every decision a tick makes is read some moments before the slot is
    claimed, and a thread the scheduler has parked is parked across those
    moments for as long as the scheduler says. A refusal that travels down
    into the lock cannot be overtaken by anything that happens in them.
    """


class DrainStopping(DrainRefused):
    """Shutdown was requested after the tick's last look at the stop event.

    ``stop()`` joins with a timeout, so a shutdown arriving mid-tick is the
    ordinary case rather than the exotic one. What this prevents is not a
    wasted drain but an analyzer subprocess spawned after the app lifespan
    that owned it has already gone.
    """


class DrainSuperseded(DrainRefused):
    """This exact queue had already been attempted by a finished drain.

    Carries the attempt that superseded the start, because that attempt is
    the thing the tick then has to book.
    """

    def __init__(self, attempt: Attempt) -> None:
        super().__init__(f"a finished drain already attempted {attempt.signature}")
        self.attempt = attempt


#: Called with the newest finished attempt WHILE the registry lock is held,
#: and refuses the start by raising :class:`DrainSuperseded`. The reading is
#: the caller's - it owns the registry - and the judgment is the watcher's,
#: which is the only side that knows what it has already booked.
GuardFn = Callable[["Attempt | None"], None]
#: Starting a drain hands back the reader for THAT drain. The watcher keeps
#: it rather than looking a job up later: the registry holds a single slot,
#: so a manual refresh taking the slot before the next tick would otherwise
#: erase the booking and relaunch the same failed backlog.
StartFn = Callable[[GuardFn], ConsumedFn]

#: Reads the newest FINISHED drain, whoever started it, or None when the
#: registry is empty. This is how an attempt made by a MANUAL drain reaches
#: the loop: it drains the same derived queue, so the loop must not repeat it.
AttemptedFn = Callable[[], Attempt | None]

#: Every outcome ``AutoAnalyzeWatcher.tick`` can report. Named so a log line
#: and a test assert on the same vocabulary.
TICK_OUTCOMES: tuple[str, ...] = (
    "disabled", "stopping", "busy", "empty", "unchanged", "started",
)


def enabled_from_environ(environ: Mapping[str, str]) -> bool:
    """Read ``MUSIC_DJ_AUTO_ANALYZE``. Fail fast on anything but on/off."""
    raw = environ.get(AUTO_ANALYZE_ENV, "on")
    if raw not in AUTO_ANALYZE_VALUES:
        raise ValueError(
            f"{AUTO_ANALYZE_ENV}={raw!r} is not valid; "
            f"expected one of {list(AUTO_ANALYZE_VALUES)}"
        )
    return raw == "on"


def arm_from_environ(environ: Mapping[str, str]) -> bool:
    """Whether the daemon should ARM the loop: asked for AND able to run.

    ``MUSIC_DJ_AUTO_ANALYZE=on`` is a request, not a capability. The packaged
    Open DJ engine installs the ``uv export --no-dev`` closure
    (``scripts/build_engine_payload.py``) while librosa and scipy live in the
    optional ``analysis`` extra, so the installed desktop app has no analyzer:
    arming there would give every first import a failed job and nothing else -
    the drain raises BackendNotAvailable, the queue stays pending, and the
    signature guard then suppresses the retries.

    Declined LOUDLY. A feature that silently does nothing is a support ticket,
    so the reason and the fix are logged once, at construction.
    """
    if not enabled_from_environ(environ):
        return False
    if backends.default_backend_installed():
        return True
    log.warning(
        "auto-analyze: %s is on but the %r backend is not installed here "
        "(needs %s), so every drain would fail with BackendNotAvailable and "
        "the reconcile loop is NOT armed. Install the analysis extra "
        "(music-dj-tools[analysis]) to enable analyze-on-import.",
        AUTO_ANALYZE_ENV,
        backends.DEFAULT_BACKEND,
        ", ".join(backends.DEFAULT_BACKEND_MODULES),
    )
    return False


@dataclass
class AutoAnalyzeState:
    """What the daemon reports about its own auto-drain."""

    enabled: bool
    interval_s: float
    attempts: int = 0
    last_started_at: float | None = None
    last_signature: str | None = None
    last_outcome: str | None = None


def build(*, enabled: bool, interval_s: float = DEFAULT_INTERVAL_S) -> AutoAnalyzeState:
    if interval_s <= 0:
        raise ValueError(f"interval_s must be > 0, got {interval_s}")
    return AutoAnalyzeState(enabled=enabled, interval_s=interval_s)


class AutoAnalyzeWatcher:
    """Reconcile loop over the unmapped backlog. One tick, one decision."""

    def __init__(
        self,
        state: AutoAnalyzeState,
        *,
        scan_fn: ScanFn,
        start_fn: StartFn,
        running_fn: RunningFn,
        attempted_fn: AttemptedFn,
    ) -> None:
        self._state = state
        self._scan = scan_fn
        self._start = start_fn
        self._running = running_fn
        self._attempted = attempted_fn
        self._consumed: ConsumedFn | None = None
        self._awaiting_drain = False
        #: The finished drain already accounted for. A strong reference on
        #: purpose: see :class:`Attempt`.
        self._booked: object | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> AutoAnalyzeState:
        return self._state

    def tick(self) -> str:
        """One reconcile step. Returns a member of :data:`TICK_OUTCOMES`."""
        outcome = self._decide()
        self._state.last_outcome = outcome
        return outcome

    def _account_for(self, attempt: Attempt | None) -> None:
        """Settle the last drain's attempt against ``last_signature``.

        Split out of :meth:`_decide` so the decision reads as the four
        questions it asks (stopped? busy? anything pending? did it change?)
        rather than as those questions with a ledger threaded through them.
        """
        if self._awaiting_drain:
            # A drain we started has finished. Book the attempt against the
            # queue the WORKER read, never against our own pre-start scan.
            # Those are two reads of a derived queue and they can differ: a
            # file that leaves the pending set in between means the worker
            # consumed a different - possibly empty - queue, and recording our
            # snapshot would retire a track it never touched. The track can
            # come back with every stat field intact - a vendor mapping that
            # arrives and is withdrawn never touches the file at all - so that
            # snapshot would then match forever, and no polling tick is
            # obliged to observe the gap for that to happen.
            self._state.last_signature = (
                self._consumed() if self._consumed is not None else None
            )
            self._awaiting_drain = False
        elif (
            attempt is not None
            and attempt.targets_backlog
            and attempt.job is not self._booked
        ):
            # A drain this loop did NOT start has finished since the last
            # tick. POST /analysis-queue/run drains exactly the queue watched
            # here, so the attempt it made is one this loop must not repeat.
            # Without this the suppression contract has a hole the size of
            # every manual drain: last_signature is None after a fresh start,
            # any non-empty queue therefore reads as changed, and the first
            # tick after a manual failure relaunches the identical queue the
            # user just watched fail.
            #
            # Qualified on SCOPE, so a library or batch job - which never
            # targeted this backlog - can never retire it. It is NOT qualified
            # on a published signature, because that skips the case this
            # branch most needs to see: a finished unmapped drain that lost a
            # target publishes None deliberately, and that None has to CLEAR a
            # booking made by some earlier attempt. Skipping it leaves the
            # older signature standing, and a target that returns with the
            # same content token then sits behind `unchanged` for good.
            #
            # Qualified on IDENTITY because the registry holds one slot and
            # keeps a finished job in it. Re-reading that job on a later tick
            # would re-book a signature the empty branch below had just
            # deliberately forgotten, and a queue that emptied and came back
            # would stall forever - the exact bug that branch exists to fix.
            self._state.last_signature = attempt.signature
        if attempt is not None and attempt.targets_backlog:
            # Booked once a FINISHED attempt on this backlog has been read -
            # whatever it published, including a deliberate None, because that
            # is a verdict and not an absence. `_attempted` never reports a
            # job whose verdict is still outstanding, which is what makes
            # identity alone the right key here.
            self._booked = attempt.job

    def _refuse_start(self, attempt: Attempt | None, queue: str) -> None:
        """Guard for :data:`StartFn`, run inside the registry's own lock.

        Both of the tick's remaining reasons NOT to start are re-asked here,
        because both were read some statements ago and a parked thread is
        parked across statements. Shutdown comes first: a drain refused for
        being redundant is a wasted subprocess, one started after the
        lifespan has gone is a subprocess nothing owns.

        A job already BOOKED does not supersede anything. It is the attempt
        this tick's own accounting is built on, and the empty-queue branch
        deliberately forgets its signature so a byte-identical queue can be
        analyzed again; refusing on it would reinstate the very suppression
        that forget exists to lift.
        """
        if self._stop.is_set():
            raise DrainStopping
        if attempt is None or not attempt.targets_backlog:
            return
        if attempt.job is self._booked or attempt.signature != queue:
            return
        raise DrainSuperseded(attempt)

    def _decide(self) -> str:
        if not self._state.enabled:
            return "disabled"
        if self._stop.is_set():
            # Checked HERE, not only at the top of the loop, because stop()
            # joins with a timeout: a scan or a residency probe over a slow
            # volume can outlast it, so the join can return while this tick
            # is still running. Without this, the decision path goes on to
            # spawn an analysis subprocess after the app lifespan has already
            # shut down.
            return "stopping"
        if self._running():
            return "busy"
        backlog = self._scan()
        if self._stop.is_set():
            # Rechecked AFTER the scan, because the scan is the slow part: a
            # residency probe over a network volume or a sleeping disk can
            # outlast stop()'s join, so the event can arrive between the check
            # above and here. Without this, a shutdown that timed out still
            # ends with this tick spawning an analysis subprocess, which is
            # the exact thing stop() was called to prevent.
            return "stopping"
        # Read AFTER the scan, not before it. The scan is the slow part of a
        # tick - it resolves and stats every unmapped track, so on a network
        # volume it is wide enough for a whole manual drain to start, run and
        # finish inside. An attempt read before it is that stale by the time
        # it is used, and a manual drain that failed per-track leaves the
        # backlog and the signature byte-for-byte where it found them, so the
        # tick would see no attempt, no change, and relaunch the identical
        # drain it just missed. Reading here leaves a window of two
        # comparisons between the ledger and _start(), which no drain can be
        # spawned and reaped inside.
        self._account_for(self._attempted())
        if backlog.pending_total == 0:
            # An empty queue has no failed attempt left to suppress, so the
            # remembered signature is forgotten here. That is what lets the
            # loop recover from a track that left the pending set BETWEEN this
            # scan and the worker's own rescan inside the job: the worker found
            # no target and finished clean, and the track can return with the
            # whole signature byte-for-byte identical, which is what a
            # withdrawn vendor mapping produces since it never touches the
            # file. Without this the queue would match
            # the recorded attempt forever and that track would never be
            # analyzed. A still-non-empty, still-unchanged queue is untouched
            # by this and is still refused below, so the retry-storm guard is
            # unchanged.
            self._state.last_signature = None
            return "empty"
        if backlog.signature == self._state.last_signature:
            return "unchanged"
        return self._launch(backlog)

    def _launch(self, backlog: Backlog) -> str:
        """Claim the slot for this backlog, or report why the guard refused.

        Split from :meth:`_decide` so the decision stays a list of questions
        and this stays the one place that acts on them.

        Arming the booking waits until the drain is actually running.
        ``_start`` raises when the analysis step is disabled (422) or when a
        manual refresh claimed the one job slot (409); arming before that
        would mark this queue "already tried", and every later tick would
        report "unchanged" until some other import moved the signature. The
        backlog would silently never drain.
        """
        try:
            self._consumed = self._start(
                lambda attempt: self._refuse_start(attempt, backlog.signature)
            )
        except DrainStopping:
            # stop() landed between the recheck after the scan and the claim.
            return "stopping"
        except DrainSuperseded as superseded:
            # A drain finished over this exact queue between the ledger read
            # and the claim. Book it and say what it means: the queue was
            # attempted, just not by this tick.
            self._account_for(superseded.attempt)
            return "unchanged"
        self._awaiting_drain = True
        self._state.attempts += 1
        self._state.last_started_at = time.time()
        log.info(
            "auto-analyze: started a drain over %d unmapped track(s)",
            backlog.pending_total,
        )
        return "started"

    # --- thread lifecycle ------------------------------------------------

    def start(self, join_timeout: float = RESTART_JOIN_S) -> None:
        """Start the reconcile thread. A no-op when disabled or already up.

        A handle kept by a timed-out :meth:`stop` is retired here once that
        thread has actually exited, so a watcher REUSED across a lifespan
        restart can start a loop again. While it is still alive, this refuses
        loudly rather than putting a second loop beside it - two loops share
        one job slot and would take turns losing a 409 race.

        A survivor is WAITED FOR first, because refusing outright was the
        worse of the two failures. That thread is not wedged: :meth:`stop`
        already set the event and it exits as soon as the scan in front of it
        returns, usually moments after the join gave up. Declining then left
        the restarted lifespan running with no reconcile loop at all, so
        nothing imported afterwards was auto-analyzed until some further
        restart - silent, and indefinite. The wait is bounded, so a genuinely
        stuck loop costs the daemon ``join_timeout`` at startup rather than
        the startup itself, and a survivor still there afterwards is refused
        exactly as before.

        The alternative - clearing the stop event and hoping the survivor
        notices before it checks - races with no warning at all.
        """
        if not self._state.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
        if self._thread is not None:
            log.warning(
                "auto-analyze: %r did not exit within %.1fs of this restart, "
                "so no second loop was started beside it and this process is "
                "left with no reconcile loop until the next start()",
                THREAD_NAME, join_timeout,
            )
            return
        # Cleared only here, where the previous loop is known to be gone: a
        # reused watcher whose stop() set it would otherwise start a thread
        # that exits on its first check.
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=THREAD_NAME, daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the loop to stop and wait for it, keeping a thread that stayed.

        A join that times out has not stopped anything. Clearing the handle
        anyway loses the only reference to a live loop, and the next
        ``start()`` - a lifespan restart in the same process - would see
        ``_thread is None`` and spawn a SECOND watcher beside it. The tick
        itself is safe either way (``_decide`` refuses once the event is
        set), so a survivor is loud rather than fatal.
        """
        self._stop.set()
        if self._thread is None:
            return
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            log.warning(
                "auto-analyze: %r did not stop within %.1fs; it will start no "
                "further drain, and the handle is kept so a later start() "
                "cannot spawn a second loop beside it",
                THREAD_NAME, timeout,
            )
            return
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - loop boundary
                # A tick failure (unreadable state.db, a 409 race) must not
                # kill the loop; it is logged and retried next interval.
                log.warning("auto-analyze tick failed: %s", exc)
            self._stop.wait(self._state.interval_s)


__all__ = [
    "AUTO_ANALYZE_ENV",
    "AUTO_ANALYZE_VALUES",
    "DEFAULT_INTERVAL_S",
    "RESTART_JOIN_S",
    "THREAD_NAME",
    "TICK_OUTCOMES",
    "Attempt",
    "AttemptedFn",
    "AutoAnalyzeState",
    "AutoAnalyzeWatcher",
    "ConsumedFn",
    "arm_from_environ",
    "build",
    "enabled_from_environ",
]
