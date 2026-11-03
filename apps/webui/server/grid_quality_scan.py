"""Scan the library's beatgrids into stored verdicts (GRIDFLAG-02).

The grid judged is the one the deck plays for v1: rekordbox's PQTZ, read with
pyrekordbox from the track's ANLZ `.DAT` (decided in
`(private path)`: decks stay on rekordbox's
grid for v1). A track with no rekordbox analysis has no grid to judge and is
`unknown`, never `ok`.

COST. Parsing one ANLZ file is about 10 ms (94.7 s for 9,659 files, measured
Thu 1 Oct 2026), so the scan is incremental: a track whose analysis file has
the same mtime and size as last time, under the same rule version, is skipped
after one `stat`. The first scan of the 1,184 present tracks is about 11 s of
parsing; a rescan with nothing changed is 1,184 stats.

It runs on its own thread (`ScanRunner`, one per app), started by `POST /beatgrid-flags/scan`
or `opendj track grid-flags --scan`, never by a list request. `pause_s`
yields between parses so the audio-serving request threads are not starved
by a pure-Python parser holding the interpreter.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 scan_library(): verdict per track, persisted.
    [if] a grid did not change since the last scan [then] it is not parsed
      ⛔️ a re-parse per scan
    [if] the analysis file changed [then] the verdict is recomputed ⛔️ stale
    [if] the rule's thresholds changed [then] every verdict is recomputed ⛔️
    [if] the file is missing or unreadable [then] `unknown` with that reason
      ⛔️ ok, suspect, or a crash that ends the scan
  ✔︎ ✅ 🎯 ScanRunner: single-flight background scan with a readable status
    (engine in scan_runner.py).
    [if] a scan is running and another is requested [then] the running one
      is reported ⛔️ two scans at once
    [if] the scan raises [then] the error is kept in the status ⛔️ silence
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox.errors import _open_ro
from apps.adapters.rekordbox.models import RbRowMeta
from apps.analysis_beatgrid.grid_quality import (
    REASON_ANLZ_MISSING,
    REASON_ANLZ_UNREADABLE,
    REASON_NO_GRID_DATA,
    REASON_NO_REKORDBOX_GRID,
    GridQuality,
    classify_grid,
    rule_version,
    unknown_quality,
)
from apps.shared.events import publish
from apps.shared.platform_paths import AssetResolver
from apps.webui.server import grid_quality_store, library_playable
from apps.webui.server.grid_quality_store import StoredGridQuality
from apps.webui.server.rb_vendor_pkg.track_rows import bulk_rb_meta
from apps.webui.server.scan_runner import ScanRunner as SharedScanRunner

log = logging.getLogger(__name__)

ScanScope = Literal["present", "all"]

GRID_SOURCE_REKORDBOX = "rekordbox"
GRID_SOURCE_NONE = "none"

#: Seconds slept after each parsed file when the scan runs in the background.
BACKGROUND_PAUSE_S = 0.005
#: Verdicts are committed in batches so a long first scan shows up as it goes.
WRITE_BATCH = 200
SCAN_THREAD_NAME = "grid-quality-scan"


@dataclass(frozen=True)
class ScanResult:
    scope: ScanScope
    #: Tracks in scope.
    considered: int
    #: Analysis files actually parsed (the expensive part).
    parsed: int
    #: Tracks whose stored verdict was still current and was left alone.
    unchanged: int
    #: Verdicts written (new or replaced).
    written: int
    duration_s: float
    finished_at: str


# --------------------------------------------------------------- helpers


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def scope_ids(scope: ScanScope) -> list[str]:
    """Stable ids in scope: tracks with audio on this machine, or every live track."""
    conn = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        if scope == "present":
            return [sid for sid, _path in library_playable.scan_playability(conn).present]
        if scope == "all":
            return [
                str(row[0])
                for row in conn.execute(
                    "SELECT stable_id FROM tracks WHERE deleted_at IS NULL ORDER BY stable_id"
                )
            ]
        raise ValueError(f"unhandled scan scope: {scope!r}")
    finally:
        conn.close()


def _grid_identity(
    meta: RbRowMeta | None, resolver: AssetResolver
) -> tuple[str, str, Path | None, str | None]:
    """`(grid_source, grid_version, dat_path, unknown_reason)` from one stat."""
    if meta is None or not meta.analysis_data_path:
        return GRID_SOURCE_NONE, "none", None, REASON_NO_REKORDBOX_GRID
    mapped = resolver.resolve_asset_path(meta.analysis_data_path)
    if mapped.resolved is None:
        return GRID_SOURCE_REKORDBOX, f"unresolved:{mapped.reason}", None, REASON_ANLZ_MISSING
    try:
        stat = mapped.resolved.stat()
    except FileNotFoundError:
        return GRID_SOURCE_REKORDBOX, "missing", None, REASON_ANLZ_MISSING
    except OSError as exc:
        # A path that exists but cannot be read (a volume gone mid-scan, a
        # pending privacy prompt). Recorded as such; retried on the next scan.
        log.warning("grid-quality: cannot stat %s (%s)", mapped.resolved, exc)
        return GRID_SOURCE_REKORDBOX, "unreadable", None, REASON_ANLZ_UNREADABLE
    # The path is part of the identity: a remap to another ANLZ file with the
    # same size and mtime is a different grid.
    path_id = hashlib.sha256(str(mapped.resolved).encode("utf-8")).hexdigest()[:16]
    return (
        GRID_SOURCE_REKORDBOX,
        f"rbx:{path_id}:{stat.st_mtime_ns}:{stat.st_size}",
        mapped.resolved,
        None,
    )


def read_pqtz_grid(dat_path: Path) -> tuple[list[float], list[float]] | None:
    """`(beat times s, per-beat bpm)` from a rekordbox ANLZ file's PQTZ tag,
    or None when the file carries no PQTZ tag."""
    # Imported here, not at module scope: the standalone engine imports this
    # module with pyrekordbox absent (STANDALONE-01) and never reaches a scan.
    from pyrekordbox.anlz import AnlzFile

    anlz = AnlzFile.parse_file(str(dat_path))
    pqtz = next((tag for tag in anlz.tags if tag.type == "PQTZ"), None)
    if pqtz is None:
        return None
    times = [round(float(value), 3) for value in pqtz.get_times()]
    bpms = [round(float(value), 2) for value in pqtz.get_bpms()]
    return times, bpms


def _judge(dat_path: Path) -> GridQuality:
    try:
        grid = read_pqtz_grid(dat_path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # The same parse failures `library_readiness._peek_pqtz` treats as an
        # invalid file. Not swallowed: the verdict records it as `unknown`
        # with this reason, and the scan goes on to the next track.
        log.warning("grid-quality: unreadable ANLZ %s (%s)", dat_path, exc)
        return unknown_quality(REASON_ANLZ_UNREADABLE)
    if grid is None:
        return unknown_quality(REASON_NO_GRID_DATA)
    return classify_grid(grid[0], grid[1])


def _stored(
    stable_id: str, source: str, version: str, rule: str, quality: GridQuality
) -> StoredGridQuality:
    return StoredGridQuality(
        stable_id=stable_id,
        grid_source=source,
        grid_version=version,
        rule_version=rule,
        computed_at=_utc_now(),
        **asdict(quality),
    )


# ------------------------------------------------------------------ scan


def scan_library(*, scope: ScanScope = "present", pause_s: float = 0.0) -> ScanResult:
    """Bring the stored verdict of every track in `scope` up to date."""
    started = time.perf_counter()
    rule = rule_version()
    ids = scope_ids(scope)
    metas = bulk_rb_meta(ids)
    stored = grid_quality_store.read_many(ids)
    resolver = AssetResolver()
    pending: list[StoredGridQuality] = []
    parsed = unchanged = written = 0
    for stable_id in ids:
        source, version, dat_path, unknown_reason = _grid_identity(metas.get(stable_id), resolver)
        previous = stored.get(stable_id)
        if (
            previous is not None
            and previous.grid_version == version
            and previous.rule_version == rule
        ):
            unchanged += 1
            continue
        if dat_path is not None:
            quality = _judge(dat_path)
            parsed += 1
            if pause_s > 0:
                time.sleep(pause_s)
        elif unknown_reason is not None:
            quality = unknown_quality(unknown_reason)
        else:
            raise AssertionError("a grid with no file must say why it is unknown")
        pending.append(_stored(stable_id, source, version, rule, quality))
        if len(pending) >= WRITE_BATCH:
            grid_quality_store.upsert_many(pending)
            written += len(pending)
            pending = []
    grid_quality_store.upsert_many(pending)
    written += len(pending)
    return ScanResult(
        scope=scope,
        considered=len(ids),
        parsed=parsed,
        unchanged=unchanged,
        written=written,
        duration_s=round(time.perf_counter() - started, 3),
        finished_at=_utc_now(),
    )


# ---------------------------------------------------------------- runner


def _publish_grid_result(result: ScanResult) -> None:
    if result.written > 0:
        # Rows carry the verdict, so listings must be read again.
        publish("library.changed", {"kind": "tracks", "ids": []})


class ScanRunner(SharedScanRunner[ScanResult]):
    """Single-flight scan with a status any caller can read."""

    def __init__(
        self,
        scan_fn: Callable[..., ScanResult] | None = None,
        *,
        thread_name: str = SCAN_THREAD_NAME,
        label: str = "grid-quality",
    ) -> None:
        # The audio-fit scan (GRIDFIT-02) reuses this runner with its own scan.
        self._scan_fn = scan_fn or scan_library
        super().__init__(
            thread_name=thread_name,
            label=label,
            on_busy="wait",
            extra_status=lambda _running, _stop, extra: {"running_scope": extra},
            on_result=_publish_grid_result,
        )

    def start(self, scope: ScanScope) -> dict[str, Any]:
        """Start a background scan unless one is running; returns the status."""
        return self.start_work(
            lambda _stop: self._scan_fn(scope=scope, pause_s=BACKGROUND_PAUSE_S),
            extra=scope,
        )

    def run_blocking(self, scope: ScanScope) -> dict[str, Any]:
        """Scan on the caller's thread; waits out a scan already running."""
        return self.run_work(
            lambda _stop: self._scan_fn(scope=scope, pause_s=0.0),
            extra=scope,
        )


_RUNNER_GUARD = threading.Lock()


def runner_for(app_state: Any) -> ScanRunner:
    """The one runner of a FastAPI app (`app.state`), created on first use, so
    two apps in one process never share a scan status."""
    with _RUNNER_GUARD:
        runner = getattr(app_state, "grid_quality_runner", None)
        if runner is None:
            runner = ScanRunner()
            app_state.grid_quality_runner = runner
        return runner

__all__ = [
    "BACKGROUND_PAUSE_S",
    "GRID_SOURCE_NONE",
    "GRID_SOURCE_REKORDBOX",
    "ScanResult",
    "ScanRunner",
    "ScanScope",
    "read_pqtz_grid",
    "runner_for",
    "scan_library",
    "scope_ids",
]
