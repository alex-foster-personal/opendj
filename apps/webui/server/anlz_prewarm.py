"""ANLZ cache prewarm: fill the parsed-ANLZ cache before a row or deck asks (PERF-UI-9702).

``rb_vendor_pkg/anlz_cache.py`` fills an entry only on the first ``/anlz``
request, so a fresh install pays the full materialisation (about 72 to 114 ms
per track) the first time each track is browsed or loaded. This pass walks the
rekordbox-mapped tracks, finds the ones with no current entry (keyed on
``_anlz_mtime`` exactly as ``build_anlz_payload`` keys it), and builds them
through ``build_anlz_payload`` itself, which writes the entry with
``_store_cached_payload``. Nothing here parses ANLZ or writes the cache file.

The pass is bounded: at most ``batch`` tracks per call, and it yields, writing
nothing, while the playing gate reports a deck playing. The ahead-analysis
drain runs it as the ``anlz_prewarm`` phase after the strip phase.

Requirements (mini-PRD):
  [if] a mapped track has no current cache entry [then] one pass writes it
  [if] a track's ANLZ files are newer than its entry [then] it is warmed again
  [if] a deck is playing [then] the pass writes nothing and reports ``yielded``
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from apps.shared import runtime_policy

log = logging.getLogger(__name__)

#: Tracks warmed per pass; one drain tick, so a deck load waits one batch at most.
PREWARM_BATCH: int = 8
#: A drained library is rescanned (one stat per mapped track) this often.
RESCAN_S: float = 60.0


@dataclass
class PrewarmStatus:
    """Progress as of the last pass: what ``GET /library/anlz-prewarm`` serves."""

    cached: int | None = None
    missing: int | None = None
    last_batch_ms: float | None = None
    state: str = "not_run"


def _default_warm(stable_id: str, points: int) -> None:
    """Build one track's payload so ``build_anlz_payload`` stores its cache entry."""
    from apps.webui.server import rb_vendor

    rb_vendor.build_anlz_payload(rb_vendor.resolve_content(stable_id), points)


def _current_mtime(stable_id: str) -> float | None:
    """The track's ANLZ mtime, or None when it has no readable ANLZ directory."""
    from apps.adapters.rekordbox.paths import anlz_dir, resolve_content
    from apps.webui.server.rb_vendor_pkg.anlz_cache import _anlz_mtime

    try:
        return _anlz_mtime(anlz_dir(resolve_content(stable_id)))
    except Exception:  # noqa: BLE001 -- an unwarmable track is skipped, never fatal
        return None


class AnlzPrewarm:
    """Bounded background filler for the ANLZ cache of rekordbox-mapped tracks."""

    def __init__(
        self,
        *,
        playing_fn: Callable[[], bool],
        warm_fn: Callable[[str, int], None] = _default_warm,
        points: int = runtime_policy.ANLZ_POINTS_DEFAULT,
        batch: int = PREWARM_BATCH,
        rescan_s: float = RESCAN_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Hold the gate, the per-track warmer and the batch bound."""
        self._playing_fn = playing_fn
        self._warm_fn = warm_fn
        self._points = points
        self._batch = batch
        self._rescan_s = rescan_s
        self._monotonic = monotonic
        #: stable_id -> the ANLZ mtime its entry is known current for; skips a JSON read.
        self._fresh: dict[str, float] = {}
        self._failed: set[str] = set()
        self._pending: list[tuple[str, float]] = []
        self._cached: int = 0
        self._scanned_at: float | None = None
        self._status = PrewarmStatus()
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        """The last pass's counts, read without running a pass."""
        with self._lock:
            s = self._status
            return {
                "cached": s.cached,
                "missing": s.missing,
                "last_batch_ms": s.last_batch_ms,
                "state": s.state,
            }

    def _is_current(self, stable_id: str, mtime: float) -> bool:
        """Whether ``stable_id`` has a cache entry current for ``mtime``."""
        from apps.webui.server.rb_vendor_pkg.anlz_cache import _load_cached_payload

        if self._fresh.get(stable_id) == mtime:
            return True
        if _load_cached_payload(stable_id, mtime, self._points) is None:
            return False
        self._fresh[stable_id] = mtime
        return True

    def _scan(self, mapped_ids: Sequence[str]) -> None:
        """Re-list which mapped tracks lack a current entry (stat-only once known fresh)."""
        pending: list[tuple[str, float]] = []
        cached = 0
        for sid in dict.fromkeys(mapped_ids):
            mtime = _current_mtime(sid)
            if mtime is None:
                continue
            if self._is_current(sid, mtime):
                cached += 1
            elif sid not in self._failed:
                pending.append((sid, mtime))
        self._pending = pending
        self._cached = cached
        self._scanned_at = self._monotonic()

    def run_pass(self, mapped_ids: Sequence[str]) -> str:
        """Warm up to one batch of ``mapped_ids`` lacking a current entry.

        The track list is rescanned only once the last scan's work is done and
        ``rescan_s`` has passed, so a drained library costs one stat per track
        a minute, not per tick. Returns ``yielded`` (a deck plays; nothing
        written), ``green`` (nothing missing) or ``ran`` (a batch was written).
        """
        if self._playing_fn():
            with self._lock:
                self._status.state = "yielded"
            return "yielded"
        due = self._scanned_at is None or self._monotonic() - self._scanned_at >= self._rescan_s
        if not self._pending and due:
            self._scan(mapped_ids)
        started = self._monotonic()
        attempted = 0
        written = 0
        outcome = "green"
        while self._pending and attempted < self._batch:
            if self._playing_fn():
                outcome = "yielded"
                break
            sid, mtime = self._pending.pop(0)
            attempted += 1
            try:
                self._warm_fn(sid, self._points)
            except Exception as exc:  # noqa: BLE001 -- one bad track never stops the pass
                log.warning("anlz prewarm failed for %s: %s", sid, exc)
                self._failed.add(sid)
                continue
            self._fresh[sid] = mtime
            written += 1
            outcome = "ran"
        self._cached += written
        with self._lock:
            last_ms = self._status.last_batch_ms
            if attempted:
                last_ms = round((self._monotonic() - started) * 1000.0, 1)
            self._status = PrewarmStatus(
                cached=self._cached, missing=len(self._pending), last_batch_ms=last_ms, state=outcome
            )
        return outcome


__all__ = ["PREWARM_BATCH", "RESCAN_S", "AnlzPrewarm", "PrewarmStatus"]
