"""Reservation guard and open-deck protection for stem hydration (ADR-0024).

Split from :mod:`apps.cloud.stem_hydration` (issue #9726), which re-exports
every name here.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from apps.cloud.eviction import HydrationError

RESERVATION_FILENAME: str = "stem-order-reserved-100.json"


# --- reservation guard (D5) ---------------------------------------------------


def reservation_file_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / RESERVATION_FILENAME


def load_reserved_ids(data_dir: Path) -> frozenset[str]:
    """The stable_ids carved out for in-app ordering tests.

    A missing file is an empty reservation, not an error: most machines,
    and every test that does not care about the guard, never write one.
    A present file with the wrong shape IS an error -- a malformed
    reservation file must never be silently read as "nothing reserved".
    """
    path = reservation_file_path(data_dir)
    if not path.is_file():
        return frozenset()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HydrationError(f"{path} is not valid JSON: {exc}") from exc
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        raise HydrationError(f"{path} must have a top-level 'tracks' array")
    return frozenset(str(entry["stable_id"]) for entry in tracks)


# --- open-deck protection ------------------------------------------------------


#: How long a bundle stays eviction-protected after the manifest or part
#: route last served it, with no explicit deck-open call required. Chosen
#: as a generous multiple of a normal DJ set length so a long-running
#: session's decks are never evicted between two real loads of the same
#: track; see (private path)
#: Addendum 3 for the full rationale.
OPEN_DECK_SERVED_TTL_S: float = 6 * 3600.0  # 6 hours


class OpenDeckRegistry:
    """Which stable_ids are currently loaded on a deck, in this process.

    Refcounted so two decks (or a deck plus a preview strip) holding the
    same track double-count correctly. Eviction consults this and a bundle
    it names is never removed regardless of recency. Deliberately in-memory
    and per-process: losing it on restart is safe because nothing is holding
    an open deck yet at that point.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[str, int] = {}
        self._served_at: dict[str, float] = {}
        #: Each read's own hold, by token, so overlapping reads of one
        #: bundle never undo each other (Codex on #6821). A read that
        #: succeeds keeps its hold until the TTL; one that fails drops it.
        self._read_holds: dict[str, dict[int, float]] = {}
        self._next_read_token: int = 0
        #: True only in the process that serves decks (the engine, set when it
        #: arms hydration). Any other process (a hydrate worker, a CLI) sees an
        #: empty registry, so it must never evict: its post-hydrate pass only
        #: refreshes the upload queue and leaves eviction to the engine.
        self.holds_decks: bool = False

    def mark_open(self, stable_id: str) -> None:
        with self._lock:
            self._open[stable_id] = self._open.get(stable_id, 0) + 1

    def mark_closed(self, stable_id: str) -> None:
        with self._lock:
            if stable_id not in self._open:
                return
            remaining = self._open[stable_id] - 1
            if remaining <= 0:
                del self._open[stable_id]
            else:
                self._open[stable_id] = remaining

    def is_open(self, stable_id: str) -> bool:
        with self._lock:
            return self._open.get(stable_id, 0) > 0

    def mark_served(self, stable_id: str, *, now: float | None = None) -> None:
        """Record that a manifest/part route just served this bundle's
        bytes, protecting it from eviction for OPEN_DECK_SERVED_TTL_S even
        with no explicit deck-open/close call -- there is currently no
        production caller of those endpoints (see ADR-0024 Addendum 3).
        ``now`` is injectable for tests; production omits it."""
        with self._lock:
            self._served_at[stable_id] = now if now is not None else time.monotonic()

    def mark_served_for_read(self, stable_id: str, *, now: float | None = None) -> Callable[[], None]:
        """Hold ``stable_id`` against eviction for a read that may fail; returns its undo.

        Each read gets its own hold, kept beside :meth:`mark_served`'s stamp
        rather than in it, so the undo removes exactly this read's hold and
        nothing else. A read that serves nothing leaves no protection behind,
        however its undo interleaves with other reads of the same bundle,
        while every read that did serve keeps its hold for the TTL (Codex on
        #6820 and #6821: a single shared stamp restored by a failed read could
        resurrect another failed read's mark). ``now`` is injectable for
        tests; production omits it.
        """
        moment = now if now is not None else time.monotonic()
        with self._lock:
            holds = self._read_holds.setdefault(stable_id, {})
            for token in [t for t, at in holds.items() if moment - at >= OPEN_DECK_SERVED_TTL_S]:
                del holds[token]
            token = self._next_read_token
            self._next_read_token += 1
            holds[token] = moment

        def withdraw() -> None:
            with self._lock:
                mine = self._read_holds.get(stable_id)
                if mine is None:
                    return
                mine.pop(token, None)
                if not mine:
                    del self._read_holds[stable_id]

        return withdraw

    def open_ids(self, *, now: float | None = None) -> frozenset[str]:
        """Explicitly-open ids, UNION any id served within the TTL. This is
        the single set every eviction caller already protects via
        ``protected=OPEN_DECKS.open_ids()`` -- widening its meaning here
        needs no changes at any of those call sites."""
        moment = now if now is not None else time.monotonic()
        with self._lock:
            recently_served = {sid for sid, at in self._served_at.items() if moment - at < OPEN_DECK_SERVED_TTL_S}
            # Expired holds go here too, not only on the next read of the
            # same id, so a long-lived engine does not keep one token per
            # read it ever served (Codex on #6820 and #6821).
            for sid in list(self._read_holds):
                holds = self._read_holds[sid]
                for token in [t for t, at in holds.items() if moment - at >= OPEN_DECK_SERVED_TTL_S]:
                    del holds[token]
                if holds:
                    recently_served.add(sid)
                else:
                    del self._read_holds[sid]
            return frozenset(self._open) | recently_served


#: Process-wide registry shared by the deck-load route and eviction.
OPEN_DECKS = OpenDeckRegistry()
