"""Background state.db maintenance: WAL checkpointing and analysis retention.

STATE-16. SQLite's own auto-checkpoint runs PASSIVE at commit time, so it
never waits for readers. With readers overlapping continuously (listing walks,
the analysis drains, coverage scans) a PASSIVE checkpoint never reaches the end
of the log, the WAL is never reset, and the file grows without bound: the v1
preview's state.db-wal reached 1.6 GB on Mon 5 Oct 2026 with a checkpoint
sequence of 0, i.e. it had not reset once since the database was opened.

This thread checkpoints with TRUNCATE whenever the WAL is over
``CFG.WAL_TRUNCATE_BYTES``. TRUNCATE waits (bounded by
``CFG.CHECKPOINT_BUSY_MS``) for readers to finish, then resets and truncates the
log. When a reader still pins the log it reports ``busy=1`` and has still
backfilled every frame it could; the next tick retries. Every result is logged
with the WAL size before and after, so a WAL that will not reset is visible.

STATE-17. Once per ``CFG.PRUNE_INTERVAL_S`` (and once at start) it deletes
superseded own-analysis rows, see :func:`apps.analysis.retention.prune_superseded`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from apps.analysis import retention

log = logging.getLogger(__name__)


# ----- config -------------------------------------------------------------------


class CFG:
    #: Seconds between WAL size checks.
    TICK_S: float = 30.0
    #: WAL size above which a TRUNCATE checkpoint is attempted.
    WAL_TRUNCATE_BYTES: int = 64 * 1024 * 1024
    #: Busy wait for the checkpoint. TRUNCATE holds the writer lock while it
    #: waits for readers, so this bounds how long other writers can queue
    #: behind it; it must stay well under the request-time busy_timeout (5 s).
    CHECKPOINT_BUSY_MS: int = 1000
    #: Seconds between superseded-analysis prunes.
    PRUNE_INTERVAL_S: float = 3600.0


# ----- checkpoint -----------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointResult:
    busy: bool
    log_frames: int
    checkpointed_frames: int
    wal_bytes_before: int
    wal_bytes_after: int


def wal_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}-wal")


def wal_bytes(db_path: Path) -> int:
    try:
        return wal_path(db_path).stat().st_size
    except FileNotFoundError:
        return 0


def should_checkpoint(wal_size: int, threshold: int) -> bool:
    return wal_size > threshold


def _open_existing(db_path: Path, busy_ms: int) -> sqlite3.Connection:
    """A plain rw handle on an EXISTING database: never creates the file and
    never runs migrations (``mode=rw`` fails on a missing file)."""
    conn = sqlite3.connect(
        f"file:{db_path}?mode=rw",
        uri=True,
        isolation_level=None,
        timeout=busy_ms / 1000.0,
        check_same_thread=False,
    )
    conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
    return conn


def checkpoint_truncate(db_path: Path, *, busy_ms: int) -> CheckpointResult:
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` and report what it did.

    SQLite reports a reader that pins the log as ``busy=1`` in the result row,
    not as an exception; a peer holding the writer lock past ``busy_ms`` raises
    ``database is locked``, which is reported the same way (busy, nothing
    checkpointed) because the next tick simply retries.
    """
    before = wal_bytes(db_path)
    conn = _open_existing(db_path, busy_ms)
    try:
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            row = (1, -1, -1)
    finally:
        conn.close()
    busy, log_frames, done = (int(v) for v in row)
    return CheckpointResult(
        busy=bool(busy),
        log_frames=log_frames,
        checkpointed_frames=done,
        wal_bytes_before=before,
        wal_bytes_after=wal_bytes(db_path),
    )


# ----- thread ---------------------------------------------------------------------


class StateMaintenance:
    def __init__(self, *, state_db_path: Path) -> None:
        self._db = Path(state_db_path)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy_streak = 0
        self._next_prune = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="state-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def tick(self) -> CheckpointResult | None:
        """One maintenance pass. Returns the checkpoint result, or None when
        no checkpoint was needed."""
        if not self._db.exists():
            return None
        if time.monotonic() >= self._next_prune:
            self._next_prune = time.monotonic() + CFG.PRUNE_INTERVAL_S
            report = retention.prune_superseded(self._db)
            log.info("state-maintenance analysis prune: %s", report)
        size = wal_bytes(self._db)
        if not should_checkpoint(size, CFG.WAL_TRUNCATE_BYTES):
            return None
        result = checkpoint_truncate(self._db, busy_ms=CFG.CHECKPOINT_BUSY_MS)
        if result.busy:
            self._busy_streak += 1
            log.warning(
                "state-maintenance wal checkpoint BUSY (streak %d): log=%d checkpointed=%d "
                "wal %d -> %d bytes; a reader still pins the log",
                self._busy_streak, result.log_frames, result.checkpointed_frames,
                result.wal_bytes_before, result.wal_bytes_after,
            )
        elif result.busy is False:
            self._busy_streak = 0
            log.info(
                "state-maintenance wal checkpoint ok: log=%d checkpointed=%d wal %d -> %d bytes",
                result.log_frames, result.checkpointed_frames,
                result.wal_bytes_before, result.wal_bytes_after,
            )
        return result

    def _run(self) -> None:
        # Wait first: a short-lived app (tests, a boot that fails) never
        # touches the database from this thread.
        while not self._stop.wait(CFG.TICK_S):
            try:
                self.tick()
            except Exception:
                # A maintenance failure must be loud but must not end the
                # thread: the next tick is the retry.
                log.exception("state-maintenance tick failed")


# ----- module-level handle ------------------------------------------------------------

_ACTIVE: dict[str, StateMaintenance] = {}


def start_for_state_db(state_db_path: Path) -> None:
    _ACTIVE["maintenance"] = StateMaintenance(state_db_path=state_db_path)
    _ACTIVE["maintenance"].start()


def stop() -> None:
    maintenance = _ACTIVE.pop("maintenance", None)
    if maintenance is not None:
        maintenance.stop()


__all__ = [
    "CFG",
    "CheckpointResult",
    "StateMaintenance",
    "checkpoint_truncate",
    "should_checkpoint",
    "start_for_state_db",
    "stop",
    "wal_bytes",
]
