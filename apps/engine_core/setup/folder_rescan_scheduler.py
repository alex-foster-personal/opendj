"""LIBM-128: the running half of continuous folder re-scan.

Shape copied deliberately from ``apps.sync_hub.scheduler.CloudSyncScheduler``,
the one other engine-lifespan background poller in this codebase: an asyncio
task owned by the lifespan (:func:`folder_rescan_lifespan`), the actual
reconcile run OFF the event loop through ``asyncio.to_thread`` because it
opens its own ``state.db`` connection and takes write transactions, and a
drain-on-stop that AWAITS an in-flight round with a bounded timeout rather
than abandoning it.

Deliberately NOT copied: CloudSync's Gig-posture / deck-playing / pressure-
shed deferrals. Those exist because a network sync round competes for
bandwidth and CPU with a live set. A folder-rescan round that finds nothing
changed is a stat-only walk over the configured roots plus one hash -- see
the perf-register row this PR adds for the measured cost -- so there is
nothing here worth pausing for a playing deck over.

Roots are the union of two lists, re-read on every wake:

- the setup record's folder-import roots (``folder_watch_roots``, or
  ``last_import.roots`` when the import was a folder import), and
- the Settings overlay's watched folders (LIBM-129, ``library_watcher_folders``
  in ``<data_dir>/state/ui-prefs.json``), so a folder added there is watched
  within one beat, with no restart, on a rekordbox setup too.

Idle when both lists are empty. The scheduler only READS the folders: every
write is a ``state.db`` row through ``StateWriter``; no audio file and no other
app's library is ever written.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import time

from apps.shared.async_scheduler import launch_scheduler_loop, sleep_until_stop
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apps.engine_core.setup import detect
from apps.engine_core.setup import record as setup_record
from apps.shared.shutdown_bound import STOP_STEPS_DEADLINE_S
from apps.shared.state import db as state_db
from apps.shared.state.ingest.folder_rescan import FolderRescanReport, reconcile_folders
from apps.shared.state.writer import StateWriter
from apps.sync_hub.scheduler import next_delay_s

log = logging.getLogger(__name__)

#: Same file the ``/api/v1/ui-prefs`` route reads and writes
#: (``apps.webui.server.routes.ui_prefs._FILENAME``); pinned by a test.
UI_PREFS_FILENAME = "ui-prefs.json"


@dataclass(frozen=True)
class FolderRescanCfg:
    #: Seconds between completed rounds when the last one found no changes.
    #: A stat-only walk plus one hash comparison (measured cost: see
    #: (private path)) is cheap enough to run once a
    #: minute indefinitely -- this IS the "bounded, documented interval" the
    #: issue's acceptance criteria ask for.
    INTERVAL_S: float = 60.0
    #: Seconds after start before the first round, out of the boot burst.
    INITIAL_DELAY_S: float = 20.0
    #: Ceiling on the failure backoff, in seconds.
    MAX_BACKOFF_S: float = 3600.0
    #: Seconds between wakes; each wake re-reads the setup record.
    BEAT_INTERVAL_S: float = 5.0
    #: Seconds stop() waits for an in-flight round before giving up on it.
    STOP_TIMEOUT_S: float = 10.0


CFG = FolderRescanCfg()

RoundOutcome = Literal["ok", "unchanged", "error"]

#: ``(writer, roots, previous_signature) -> report``; the production value
#: is :func:`reconcile_folders`. A test seam only -- never mocked in
#: production.
ReconcileFn = Callable[[StateWriter, list[Path], str], FolderRescanReport]


def _production_reconcile(
    writer: StateWriter, roots: list[Path], previous_signature: str
) -> FolderRescanReport:
    return reconcile_folders(writer, roots, previous_signature=previous_signature)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


class FolderRescanScheduler:
    """One per data dir, per process. Start and stop it from the event loop."""

    def __init__(
        self,
        data_dir: Path,
        *,
        cfg: FolderRescanCfg = CFG,
        reconcile_fn: ReconcileFn = _production_reconcile,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._cfg = cfg
        self._reconcile_fn = reconcile_fn
        self._signature = ""
        self._next_due = 0.0
        self._last_record_error: str | None = None
        self._last_prefs_error: str | None = None
        self._stopping: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self.consecutive_failures = 0
        self.rounds_started = 0
        self.rounds_completed = 0
        self.last_report: FolderRescanReport | None = None
        self.last_cycle_at: str | None = None

    # ----- one round (worker thread) --------------------------------------

    def run_round(self, roots: list[Path]) -> RoundOutcome:
        """Open a fresh connection, reconcile once, and journal the result.

        A fresh connection per round (not a held-open one) matches
        ``apps.shared.state.ingest.folder.run_cli``'s own pattern: this
        scheduler's thread must never hold a write connection open across
        the sleep between rounds, competing with request-serving connections
        for no reason.
        """
        self.rounds_started += 1
        state_path = self._data_dir / "state" / detect.STATE_DB_NAME
        conn: Any | None = None
        try:
            conn = state_db.open_rw(state_path)
            writer = StateWriter(conn, actor="folder-rescan")
            try:
                report = self._reconcile_fn(writer, roots, self._signature)
            finally:
                writer.close()
        except Exception:
            self.consecutive_failures += 1
            log.exception("folder-rescan round failed (%d in a row)", self.consecutive_failures)
            return "error"
        finally:
            if conn is not None:
                conn.close()
            self.rounds_completed += 1
            self._next_due = time.monotonic() + next_delay_s(
                self._cfg.INTERVAL_S, self.consecutive_failures, self._cfg.MAX_BACKOFF_S
            )
        self.consecutive_failures = 0
        self._signature = report.signature
        self.last_report = report
        self.last_cycle_at = _now_iso()
        if report.warning is not None:
            log.warning("folder-rescan: %s", report.warning)
        return "unchanged" if report.skipped_no_changes else "ok"

    # ----- the loop (event loop) ------------------------------------------

    async def start(self) -> None:
        self._stopping, self._task, self._next_due = launch_scheduler_loop(
            existing_task=self._task,
            twice_message="FolderRescanScheduler.start() called twice",
            initial_delay_s=self._cfg.INITIAL_DELAY_S,
            loop=self._loop,
            task_name="folder-rescan-scheduler",
            on_death=_log_loop_death,
        )

    async def stop(self, within_s: float | None = None) -> None:
        """Stop waking and wait for an in-flight round.

        ``within_s`` bounds that wait (W43, the engine's quit): a round still
        in flight then is logged and left to end with the process.
        """
        if self._task is None or self._stopping is None:
            return
        self._stopping.set()
        try:
            done, _ = await asyncio.wait({self._task}, timeout=within_s)
            if done:
                await self._task
            else:
                log.warning(
                    "folder rescan: round still in flight %.1fs after stop; not waiting",
                    within_s,
                )
        finally:
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _roots(self) -> list[Path] | None:
        """The folder-import roots on record, or ``None`` while idle.

        Re-read every wake (like CloudSync's ``_effective()``), so a wizard
        run that completes AFTER boot is picked up without a restart.
        """
        try:
            saved = setup_record.read(self._data_dir)
        except setup_record.SetupRecordError as exc:
            if str(exc) != self._last_record_error:
                log.error("folder-rescan scheduler idle: %s", exc)
            self._last_record_error = str(exc)
            return None
        self._last_record_error = None
        last_import = saved.last_import
        roots = list(saved.folder_watch_roots)
        if not roots and last_import is not None and last_import.get("kind") == "folder":
            roots = list(last_import.get("roots", []))
        roots += self._settings_roots()
        unique = list(dict.fromkeys(str(Path(root).expanduser()) for root in roots))
        if not unique:
            return None
        return [Path(root) for root in unique]

    def _settings_roots(self) -> list[str]:
        """Settings > watched folders (LIBM-129), or ``[]`` with a logged reason.

        A malformed prefs file must not stop the setup roots from being
        watched, so it is logged once per distinct error and read as empty.
        """
        path = self._data_dir / "state" / UI_PREFS_FILENAME
        value, error = _read_watched_folders(path)
        if error is not None:
            self._note_prefs_error(error)
            return []
        self._last_prefs_error = None
        relative = [item for item in value if item.strip() and not Path(item).expanduser().is_absolute()]
        if relative:
            # Save-time validation refuses these; a hand-edited file must not
            # make the engine scan a folder relative to its own working dir.
            self._note_prefs_error(f"{path}: skipping non-absolute watched folders {relative}")
        return [item for item in value if item.strip() and Path(item).expanduser().is_absolute()]

    def _note_prefs_error(self, message: str) -> None:
        if message != self._last_prefs_error:
            log.error("folder-rescan: ignoring Settings watched folders: %s", message)
        self._last_prefs_error = message

    async def _loop(self) -> None:
        in_flight: asyncio.Task[RoundOutcome] | None = None
        try:
            while not self._stopped():
                if in_flight is not None and in_flight.done():
                    in_flight.result()
                    in_flight = None
                roots = self._roots()
                if (
                    roots is not None
                    and in_flight is None
                    and time.monotonic() >= self._next_due
                ):
                    in_flight = asyncio.create_task(asyncio.to_thread(self.run_round, roots))
                await self._sleep(self._cfg.BEAT_INTERVAL_S)
        finally:
            if in_flight is not None:
                await self._drain(in_flight)

    def _stopped(self) -> bool:
        return self._stopping is not None and self._stopping.is_set()

    async def _sleep(self, seconds: float) -> None:
        await sleep_until_stop(self._stopping, seconds)

    async def _drain(self, in_flight: asyncio.Task[RoundOutcome]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(in_flight), timeout=self._cfg.STOP_TIMEOUT_S)
        except TimeoutError:
            log.warning(
                "folder-rescan round still in flight after %ss; its thread finishes on its own",
                self._cfg.STOP_TIMEOUT_S,
            )

    def status(self) -> dict[str, Any]:
        """The shape ``GET /api/v1/setup/status`` surfaces as ``folder_watch``."""
        report = self.last_report
        return {
            "running": self.running,
            "interval_s": self._cfg.INTERVAL_S,
            "last_cycle_at": self.last_cycle_at,
            "consecutive_failures": self.consecutive_failures,
            "warning": report.warning if report is not None else None,
            "unreadable_roots": report.unreadable_roots if report is not None else [],
            "tracks_added_last_cycle": report.tracks_added if report is not None else 0,
            "tracks_removed_last_cycle": report.tracks_removed if report is not None else 0,
        }


def _log_loop_death(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error("folder-rescan scheduler loop died", exc_info=error)


@asynccontextmanager
async def folder_rescan_lifespan(data_dir: Path) -> AsyncIterator[FolderRescanScheduler]:
    """Run the scheduler for the life of an app.

    Unlike CloudSync's, this scheduler has no hub/spoke distinction to skip:
    every engine that can run a folder import benefits from watching it.
    """
    scheduler = FolderRescanScheduler(data_dir)
    await scheduler.start()
    try:
        yield scheduler
    finally:
        await scheduler.stop(within_s=STOP_STEPS_DEADLINE_S)


__all__ = [
    "CFG",
    "FolderRescanCfg",
    "FolderRescanScheduler",
    "RoundOutcome",
    "folder_rescan_lifespan",
]


def _read_watched_folders(path: Path) -> tuple[list[str], str | None]:
    """``library_watcher_folders`` from the prefs file, or ``([], reason)``.

    A missing file is no folders and no error.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as exc:
        return [], f"cannot read {path}: {exc}"
    value = raw.get("library_watcher_folders", []) if isinstance(raw, dict) else None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return [], f"{path}: library_watcher_folders is not a list of strings"
    return value, None
