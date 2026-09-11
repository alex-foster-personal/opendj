"""Auto-index lyrics in the background, yielding to a live UI (Part 2 of #935).

The lyric-search index (:mod:`apps.lyrics.search_index`) is built one bounded
batch at a time by :class:`apps.lyrics.index_job.LyricIndexJob`. This module
owns the DAEMON half of that job: the reconcile thread the webui lifespan
runs, and the UI-activity probe that pauses it.

Each :meth:`LyricIndexWatcher.tick` is one poll. The probe it hands the job
answers "is a client in use right now?" from the usage store
(``UsageStore.snapshot()["summary"]["any_client_in_use"]``): a desktop shell
that has heartbeat recently with its page visible, or ordinary traffic from
an app user agent. While that is true the job reports ``paused`` and indexes
nothing; the moment it goes false the job resumes exactly where the on-disk
checkpoint says it stopped. So indexing pauses within one poll of the user
returning to the UI and resumes when they leave it - the DJ-safe behavior
#935 asks for (heavy index work never runs while someone could be at the
decks).

``MUSIC_DJ_LYRIC_INDEX`` is a fail-fast enum, ``on`` (the default) or
``off``. Only the real daemon entry point reads it
(``app.py:_build_default_app``); ``create_app`` takes ``lyric_index=False``
so no test builds a thread.

Requirements (mini-PRD):
  ✔︎ ✅ tick(): one poll - paused when the UI is active, else one indexed batch.
    [if] the UI is active [then] the tick reports paused and indexes nothing ⛔️
    [if] the UI is idle and work is pending [then] the tick indexes one batch ⛔️
  ✔︎ ✅ start()/stop(): the lifespan runs and joins a real thread named
    THREAD_NAME.
    [if] the watcher is disabled [then] start() starts no thread ⛔️
  ✔︎ ✅ a raising tick never reports a stale success (issue #1343 round-3
    review): last_outcome reflects the failure, and a frozen bulk-removal
    guard is logged with its exact recovery command.
    [if] a tick raises [then] last_outcome becomes FAILED_OUTCOME before the
      loop logs and retries, never the previous outcome ⛔️
    [if] the raise is LyricsCacheUnavailable [then] the loop logs the exact
      ``python -m apps.lyrics index --force-rebuild --data-dir`` command an
      operator runs to recover ⛔️
  ✔︎ ✅ wait_for_outcome(): a caller blocks on the tick recording an outcome,
    never on a wall-clock poll, so a slow runner cannot fake a failure.
    [if] a tick records an outcome [then] the waiter wakes with that value ⛔️
    [if] no tick records within the timeout [then] it returns None, which a
      caller reports as a hang, distinct from a wrong outcome ⛔️
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from apps.lyrics.index_job import TICK_OUTCOMES, LyricIndexJob
from apps.lyrics.search_index import DEFAULT_BATCH_MAX_DOCS, LyricsCacheUnavailable

log = logging.getLogger(__name__)

LYRIC_INDEX_ENV: str = "MUSIC_DJ_LYRIC_INDEX"
LYRIC_INDEX_VALUES: tuple[str, ...] = ("on", "off")
DEFAULT_INTERVAL_S: float = 1.0
RESTART_JOIN_S: float = 5.0
#: Name of the reconcile thread. A test asserts the daemon really started and
#: really stopped one by looking for it in ``threading.enumerate()``.
THREAD_NAME: str = "webui.lyric-index"
#: ``last_outcome`` when a tick raised, distinct from every value
#: :data:`apps.lyrics.index_job.TICK_OUTCOMES` can report - so a raising tick
#: is never mistaken for the last successful poll (issue #1343 round-3
#: review).
FAILED_OUTCOME: str = "failed"


@dataclass
class LyricIndexState:
    """What the daemon reports about its own auto-index loop."""

    enabled: bool
    interval_s: float = DEFAULT_INTERVAL_S
    max_docs: int = DEFAULT_BATCH_MAX_DOCS
    polls: int = 0
    last_outcome: str | None = None


def build(
    *,
    enabled: bool,
    interval_s: float = DEFAULT_INTERVAL_S,
    max_docs: int = DEFAULT_BATCH_MAX_DOCS,
) -> LyricIndexState:
    if interval_s <= 0:
        raise ValueError(f"interval_s must be > 0, got {interval_s}")
    if max_docs <= 0:
        raise ValueError(f"max_docs must be > 0, got {max_docs}")
    return LyricIndexState(enabled=enabled, interval_s=interval_s, max_docs=max_docs)


def enabled_from_environ(environ: Mapping[str, str]) -> bool:
    """Read ``MUSIC_DJ_LYRIC_INDEX``. Fail fast on anything but on/off."""
    raw = environ.get(LYRIC_INDEX_ENV, "on")
    if raw not in LYRIC_INDEX_VALUES:
        raise ValueError(
            f"{LYRIC_INDEX_ENV}={raw!r} is not valid; expected one of {list(LYRIC_INDEX_VALUES)}"
        )
    return raw == "on"


class LyricIndexWatcher:
    """Reconcile loop over the lyric cache. One tick, one poll, one batch.

    ``activity_fn`` answers "is the UI active right now?" and is handed to
    the job, so a poll while the user is at the UI indexes nothing.
    """

    def __init__(
        self,
        state: LyricIndexState,
        *,
        data_dir: Path,
        activity_fn: Callable[[], bool],
    ) -> None:
        self._state = state
        self._stop = threading.Event()
        self._outcome_recorded = threading.Condition()
        self._job = LyricIndexJob(
            data_dir,
            enabled=state.enabled,
            max_docs=state.max_docs,
            activity=activity_fn,
            should_stop=self._stop.is_set,
        )
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> LyricIndexState:
        return self._state

    def tick(self) -> str:
        """One poll step. Returns a member of :data:`TICK_OUTCOMES`.

        A raising job tick sets ``last_outcome`` to :data:`FAILED_OUTCOME`
        before re-raising, so a caller (or a status route) that reads
        ``state.last_outcome`` right after this raises never reads the
        previous success - which is what happened before this poll.
        """
        self._state.polls += 1
        try:
            outcome = self._job.tick()
        except Exception:
            self._record(FAILED_OUTCOME)
            raise
        self._record(outcome)
        return outcome

    def _record(self, outcome: str) -> None:
        """Set ``last_outcome`` and wake every :meth:`wait_for_outcome` caller."""
        with self._outcome_recorded:
            self._state.last_outcome = outcome
            self._outcome_recorded.notify_all()

    def wait_for_outcome(self, timeout: float) -> str | None:
        """Block until a tick has recorded ``last_outcome``, then return it.

        Returns None when no tick recorded one within ``timeout``: the thread
        is hung or never started, a different failure from a tick that
        recorded the wrong outcome. ``timeout`` is a hang guard, not a
        budget - the wait returns the moment a tick records.
        """
        with self._outcome_recorded:
            self._outcome_recorded.wait_for(
                lambda: self._state.last_outcome is not None, timeout
            )
            return self._state.last_outcome

    # --- thread lifecycle ------------------------------------------------

    def start(self, join_timeout: float = RESTART_JOIN_S) -> None:
        """Start the reconcile thread. A no-op when disabled or already up."""
        if not self._state.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)
            if self._thread.is_alive():
                log.warning(
                    "lyric-index: %r did not exit within %.1fs of a restart, "
                    "so no second loop was started beside it",
                    THREAD_NAME,
                    join_timeout,
                )
                return
        self._thread = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the loop to stop and wait for it, keeping a thread that stayed."""
        self._stop.set()
        if self._thread is None:
            return
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            log.warning(
                "lyric-index: %r did not stop within %.1fs; it will start no "
                "further batch, and the handle is kept so a later start() "
                "cannot spawn a second loop beside it",
                THREAD_NAME,
                timeout,
            )
            return
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except LyricsCacheUnavailable as exc:
                # The bulk-removal guard tripped (or the cache dir vanished):
                # this loop never force-rebuilds on its own, so it is frozen
                # until an operator confirms the shrinkage is real and runs
                # the recovery command below - log it loudly, every poll,
                # rather than blending into the generic warning below.
                log.error(
                    "lyric-index: frozen - %s; recover with: "
                    "python -m apps.lyrics index --force-rebuild --data-dir %s",
                    exc,
                    self._job.data_dir,
                )
            except Exception as exc:  # noqa: BLE001 - loop boundary
                # A tick failure (an unreadable cache file, a locked index db)
                # must not kill the loop; it is logged and retried next poll.
                log.warning("lyric-index tick failed: %s", exc)
            self._stop.wait(self._state.interval_s)


__all__ = [
    "DEFAULT_INTERVAL_S",
    "FAILED_OUTCOME",
    "LYRIC_INDEX_ENV",
    "LYRIC_INDEX_VALUES",
    "RESTART_JOIN_S",
    "THREAD_NAME",
    "TICK_OUTCOMES",
    "LyricIndexState",
    "LyricIndexWatcher",
    "build",
    "enabled_from_environ",
]
