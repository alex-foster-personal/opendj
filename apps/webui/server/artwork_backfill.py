"""Online cover lookup for newly ingested tracks (LIBM-187).

A track added by a folder drop or an agent's batch (``POST
/ingest/batch/{batch}/materialize``) often carries only a title and an
artist: no embedded picture, no cover image beside it. The ``/artwork``
route looks such a track up online only when a deck asks (``online=true``);
the library grid never does, so before this the track stayed blank in every
listing until it happened to be loaded on a deck.

Once an ingest batch's playback essentials are settled, the ingest
enrichment coordinator (``ingest_enrichment``, ENRICH-07) queues its
tracks here and ONE background thread runs the same chain the deck runs (embedded, then
folder image, then cache, then MusicBrainz + Cover Art Archive) once per
track, caching what it finds, and any miss, in the app's own artwork cache. This is the cover-art step of beets' fetchart and
MusicBrainz Picard, run after import. One worker, because MusicBrainz allows
one request a second anyway; the ingest request never waits for it. Every
track that ends without a cover gets one log line naming why.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from apps.shared import artwork_sources

log = logging.getLogger(__name__)

QUEUE_MAX = 5000

OutcomeStatus = Literal["has_cover", "found", "suggested", "miss", "skipped", "deferred", "error"]


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    reason: str


def cover_outcome(stable_id: str) -> Outcome:
    """Run the artwork chain once for one track, online included, and say what happened."""
    from apps.adapters.rekordbox import paths

    if paths.local_artwork_available(stable_id):
        return Outcome("has_cover", "embedded picture, folder image or cached online cover")
    query = paths.online_track_query(stable_id)
    if not artwork_sources.online_enabled():
        return Outcome("skipped", "online lookups are off (ODJ_ARTWORK_ONLINE=0)")
    if not artwork_sources.query_title(query.title) or not artwork_sources.query_artist(query.artist):
        return Outcome("skipped", f"no title and artist to search by (title={query.title!r}, "
                                  f"artist={query.artist!r})")
    cache = paths.online_artwork_cache()
    if artwork_sources.online_cover(cache, stable_id, query) is not None:
        return Outcome("found", "online cover cached")
    suggestion = cache.suggestion(stable_id)
    if suggestion is not None:
        return Outcome("suggested", f"{suggestion.source} has a cover; it is saved only if you say so "
                                    "(LIBM-188)")
    reason = cache.miss_reason(stable_id)
    if reason is not None:
        return Outcome("miss", f"{reason} (title={query.title!r}, artist={query.artist!r})")
    return Outcome("deferred", "lookup did not finish (network unreachable or MusicBrainz busy); "
                               "the next deck load retries it")


class CoverBackfill:
    """A bounded queue of stable ids and the one daemon thread that drains it."""

    def __init__(self, run_one: Callable[[str], Outcome] = cover_outcome,
                 maxsize: int = QUEUE_MAX,
                 on_outcome: Callable[[str, Outcome], None] | None = None) -> None:
        self._run_one = run_one
        self._on_outcome = on_outcome
        self._queue: queue.Queue[str] = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._start_guard = threading.Lock()

    def schedule(self, stable_ids: Iterable[str]) -> int:
        """Queue ``stable_ids`` without blocking; returns how many were queued."""
        queued = 0
        for stable_id in stable_ids:
            try:
                self._queue.put_nowait(stable_id)
            except queue.Full:
                log.warning("artwork backfill: queue full (%d), %s not queued; "
                            "a deck load still looks it up", self._queue.maxsize, stable_id)
                continue
            queued += 1
        if queued:
            self._ensure_worker()
        return queued

    def join(self) -> None:
        """Block until every queued id was processed (tests and CLI)."""
        self._queue.join()

    def _ensure_worker(self) -> None:
        with self._start_guard:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._drain, name="artwork-backfill", daemon=True)
                self._thread.start()

    def _drain(self) -> None:
        while True:
            stable_id = self._queue.get()
            try:
                self._process(stable_id)
            finally:
                self._queue.task_done()

    def _process(self, stable_id: str) -> None:
        try:
            outcome = self._run_one(stable_id)
        except Exception as exc:
            log.exception("artwork backfill %s: lookup raised", stable_id)
            outcome = Outcome("error", f"lookup raised {type(exc).__name__}: {exc}")
        if self._on_outcome is not None:
            self._on_outcome(stable_id, outcome)
        if outcome.status == "error":
            return
        if outcome.status in ("has_cover", "found", "suggested"):
            log.info("artwork backfill %s: %s (%s)", stable_id, outcome.status, outcome.reason)
        else:
            log.warning("artwork backfill %s: no cover, %s: %s",
                        stable_id, outcome.status, outcome.reason)

