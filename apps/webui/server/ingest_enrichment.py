"""Ingest enrichment coordinator: after any import, fill what each new track lacks (ENRICH-06..09).

Industry analogue: the beets import pipeline and MusicBrainz Picard, a
prioritised job graph run after import. Open DJ already has the workers; this
module decides ORDER and says what is missing:

* Workers it drives, never re-implements: the ahead-of-time analysis drain
  (``ahead_analysis``: file tags, Preview strip, loudness, beatgrid, key,
  waveform; bounded, yields to playing decks and xruns) and the one-thread
  online cover lookup (``artwork_backfill``, MusicBrainz's one request a
  second).
* A batch is the set of tracks one import added. ``POST /ingest/batch/{b}/
  materialize`` registers its batch by name; every other import path (the
  watched-folder rescan, the first-run wizard, the folder-ingest CLI) is
  picked up by a rowid watermark on ``tracks``, the way ``analysis_autostart``
  reconciles instead of subscribing: any import path, no hook to forget.
* Priority, fixed (:data:`LANE_PRIORITY`): the playback essentials (tags,
  waveform, beatgrid, key, loudness) for EVERY track of a batch first, by
  putting the batch at the front of the analysis drain behind deck bumps;
  then artwork, which for a batch never starts until every track in it has
  settled all its essentials (done, or a named failure, decline, placeholder
  or unavailable lane). Stems are never auto-queued by the app, so not here.
  Artwork also waits while a deck plays (the drain's own deck gate).
* Capability check (:func:`capability_warnings`): a lane that cannot run
  because a provider, key or switch is not configured is reported as a
  warning naming what is missing and where to set it, served on the enrich
  card and logged once, never a silent skip.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 ENRICH-06 every import is enriched in priority order
    [if] a batch registers [then] its ids go to the front of the analysis drain
    [if] rows appear from another import path [then] they become an auto batch on the next tick
    [if] a named batch registers ids an auto batch holds [then] they move to the named batch
  ✔︎ ✅ 🎯 ENRICH-07 artwork waits for the essentials
    [if] any track of a batch still lacks a waveform (or another essential) [then] no cover lookup starts for it
    [if] every essential is settled for every track [then] the batch's lookups are queued once
    [if] a deck is playing [then] no batch is released to artwork
  ✔︎ ✅ 🎯 ENRICH-08 nothing missing is silent
    [if] online lookups are off, a lane is unavailable, the drain is off or AI keys are missing [then] a warning names it and where to fix it
    [if] the same warning is seen again [then] it is not logged again
  ✔︎ ✅ 🎯 ENRICH-09 agent parity
    [if] status is asked [then] HTTP, CLI and MCP read the same document
    [if] a rerun names a playlist or batch [then] its failures and cached cover misses are forgotten and it is enriched again
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from apps.webui.server.artwork_backfill import CoverBackfill, Outcome

log = logging.getLogger(__name__)

ESSENTIAL_LANES: tuple[str, ...] = ("tags", "waveform", "beatgrid", "key", "loudness")
ARTWORK_LANE: str = "artwork"
LANE_PRIORITY: tuple[str, ...] = (*ESSENTIAL_LANES, ARTWORK_LANE)
#: Own-analysis backend per essential lane (``ahead_analysis.LANE_ORDER``).
LANE_BACKENDS: dict[str, str] = {
    "loudness": "own_loudness.backfill",
    "beatgrid": "own_beatgrid.backfill",
    "key": "own_key.backfill",
    "waveform": "own_waveform.backfill",
}
LaneState = Literal["done", "pending", "failed", "declined", "placeholder", "unavailable", "absent", "suggested"]
#: States after which a lane will not change without a rerun or a fix.
SETTLED: frozenset[str] = frozenset({"done", "failed", "declined", "placeholder", "unavailable", "absent", "suggested"})
ArtworkStage = Literal["waiting", "queued"]
MAX_BATCHES: int = 50
TICK_INTERVAL_S: float = 5.0
STORE_FILENAME: str = "ingest-enrichment.json"
AUTO_PREFIX: str = "auto-"
THREAD_NAME: str = "webui.ingest-enrichment"


@dataclass
class Batch:
    batch_id: str
    source: str
    stable_ids: list[str]
    registered_at: float
    artwork: ArtworkStage = "waiting"


@dataclass(frozen=True)
class EnrichWarning:
    lane: str
    code: str
    message: str
    fix: str
    #: "blocking": an automatic lane cannot run. "info": an opt-in lane (AI genre) cannot.
    severity: Literal["blocking", "info"] = "blocking"


@dataclass(frozen=True)
class LaneFacts:
    """One read of everything the essential lanes depend on, for a set of ids."""

    present: set[str]
    blank_tags: set[str]
    mapped: set[str]
    has_strip: set[str]
    done: Mapping[str, set[str]]
    declined: Mapping[str, Mapping[str, str]]
    placeholder: set[str]
    failures: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    unavailable: Mapping[str, str] = field(default_factory=dict)
    drain_running: bool = True


# -----------------------------------------------------------------------------
# pure policy
# -----------------------------------------------------------------------------
def essential_states(ids: Sequence[str], facts: LaneFacts) -> dict[str, dict[str, tuple[str, str | None]]]:
    """sid -> lane -> (state, reason) for the essential lanes."""
    out: dict[str, dict[str, tuple[str, str | None]]] = {}
    for sid in ids:
        if sid not in facts.present:
            out[sid] = {lane: ("absent", "no playable file here") for lane in ESSENTIAL_LANES}
            continue
        out[sid] = {lane: _lane_state(sid, lane, facts) for lane in ESSENTIAL_LANES}
    return out


def _lane_state(sid: str, lane: str, facts: LaneFacts) -> tuple[str, str | None]:
    done = _lane_done(sid, lane, facts)
    if lane == "beatgrid" and done and sid in facts.placeholder:
        return "placeholder", "own beatgrid is a placeholder (confidence 0)"
    declined = facts.declined.get(lane, {}).get(sid)
    if done and declined is not None:
        return "declined", declined
    if done:
        return "done", None
    for failed_lane in ("strip", lane) if lane == "waveform" else (lane,):
        reason = facts.failures.get(failed_lane, {}).get(sid)
        if reason is not None:
            return "failed", reason
    if not facts.drain_running:
        return "unavailable", "the analysis drain is not running"
    if lane in facts.unavailable:
        return "unavailable", facts.unavailable[lane]
    return "pending", None


def _lane_done(sid: str, lane: str, facts: LaneFacts) -> bool:
    if lane == "tags":
        return sid not in facts.blank_tags
    own = sid in facts.done.get(LANE_BACKENDS[lane], set())
    if lane == "waveform":
        return own and (sid in facts.mapped or sid in facts.has_strip)
    return own


def batch_settled(states: Mapping[str, Mapping[str, tuple[str, str | None]]]) -> bool:
    """True when every essential of every track is settled: artwork may start."""
    return all(state in SETTLED for lanes in states.values() for state, _r in lanes.values())


def lane_counts(
    rows: Mapping[str, Mapping[str, tuple[str, str | None]]], lanes: Sequence[str]
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {lane: {} for lane in lanes}
    for per_lane in rows.values():
        for lane in lanes:
            state = per_lane[lane][0]
            counts[lane][state] = counts[lane].get(state, 0) + 1
    return counts


def capability_warnings(
    *,
    drain_running: bool,
    unavailable: Mapping[str, str],
    artwork_online: bool,
    ai_missing_keys: Sequence[str],
) -> list[EnrichWarning]:
    """What cannot run here and why, each naming where to fix it (ENRICH-08)."""
    warnings: list[EnrichWarning] = []
    if not drain_running:
        warnings.append(
            EnrichWarning(
                "analysis",
                "ANALYSIS_DRAIN_OFF",
                "Waveform, BPM/beatgrid, key and loudness are not being filled in: the background analysis is off.",
                "Start Open DJ without MUSIC_DJ_AHEAD_ANALYSIS=off.",
            )
        )
    for lane, reason in sorted(unavailable.items()):
        warnings.append(
            EnrichWarning(
                lane,
                "LANE_UNAVAILABLE",
                f"{lane} analysis cannot run on this computer: {reason}",
                "Fix the named cause, then press Retry on the enrich card.",
            )
        )
    if not artwork_online:
        warnings.append(
            EnrichWarning(
                ARTWORK_LANE,
                "ARTWORK_ONLINE_OFF",
                "Missing covers are not looked up online (MusicBrainz / Cover Art Archive): online lookup is off.",
                "Remove ODJ_ARTWORK_ONLINE=0 from the environment Open DJ starts with, then restart it.",
            )
        )
    if ai_missing_keys:
        warnings.append(
            EnrichWarning(
                "genre",
                "AI_GENRE_KEYS_MISSING",
                f"AI genre tagging cannot run: {', '.join(ai_missing_keys)} not set.",
                "Set them in the environment Open DJ starts with; Settings > Genre inference lists them.",
                "info",
            )
        )
    return warnings


# -----------------------------------------------------------------------------
# coordinator
# -----------------------------------------------------------------------------
@dataclass
class EnrichmentSources:
    """The coordinator's view of the engine, injected so tests need no engine."""

    facts_fn: Callable[[Sequence[str]], LaneFacts]
    #: rowid watermark -> (stable_ids of live rows above it, new watermark).
    new_rows_fn: Callable[[int], tuple[list[str], int]]
    #: playlist_id -> its live member stable_ids; raises KeyError when unknown.
    playlist_ids_fn: Callable[[str], list[str]]
    warnings_fn: Callable[[], list[EnrichWarning]]
    playing_fn: Callable[[], bool]
    #: The drain's prioritise / retry hooks; None when the drain is not running.
    prioritize_fn: Callable[[Sequence[str]], None] | None
    retry_fn: Callable[[Sequence[str]], None] | None
    #: Forget cached cover misses so a rerun looks them up again.
    forget_misses_fn: Callable[[Sequence[str]], None]
    store_path: Path | None = None


class EnrichmentCoordinator:
    def __init__(
        self, sources: EnrichmentSources, *, covers: CoverBackfill | None = None, clock: Callable[[], float] = time.time
    ) -> None:
        self._src = sources
        self._clock = clock
        self._covers = covers or CoverBackfill(on_outcome=self._record_cover)
        self._lock = threading.RLock()
        self._batches: dict[str, Batch] = {}
        self._watermark: int | None = None
        self._cover_outcomes: dict[str, Outcome] = {}
        self._warned: set[tuple[str, str]] = set()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # --- intake -------------------------------------------------------------
    def register(self, batch_id: str, stable_ids: Iterable[str], source: str) -> Batch:
        """Record an import batch and put it at the front of the analysis drain."""
        ids = list(dict.fromkeys(stable_ids))
        with self._lock:
            for other in list(self._batches.values()):
                if other.batch_id.startswith(AUTO_PREFIX) and other.artwork == "waiting":
                    other.stable_ids = [sid for sid in other.stable_ids if sid not in set(ids)]
                    if not other.stable_ids:
                        del self._batches[other.batch_id]
            batch = Batch(batch_id, source, ids, self._clock())
            self._batches.pop(batch_id, None)
            self._batches[batch_id] = batch
            while len(self._batches) > MAX_BATCHES:
                del self._batches[next(iter(self._batches))]
            self._save()
        if self._src.prioritize_fn is not None and ids:
            self._src.prioritize_fn(ids)
        log.info("ingest enrichment: batch %s (%s) registered with %d tracks", batch_id, source, len(ids))
        self._wake.set()
        return batch

    def reconcile_new_rows(self) -> Batch | None:
        """Turn rows any other import path added since the watermark into an auto batch."""
        with self._lock:
            if self._watermark is None:
                _ids, self._watermark = self._src.new_rows_fn(-1)  # first sight: the library as it is
                self._save()
                return None
            ids, mark = self._src.new_rows_fn(self._watermark)
            known = {sid for batch in self._batches.values() for sid in batch.stable_ids}
            fresh = [sid for sid in ids if sid not in known]
            self._watermark = mark
            self._save()
        if not fresh:
            return None
        return self.register(f"{AUTO_PREFIX}{int(self._clock())}", fresh, "import")

    # --- one tick -------------------------------------------------------------
    def tick(self) -> list[str]:
        """Pick up new rows, release settled batches to artwork; returns released batch ids."""
        self.reconcile_new_rows()
        self._log_new_warnings()
        if self._src.playing_fn():
            return []
        with self._lock:
            waiting = [batch for batch in self._batches.values() if batch.artwork == "waiting"]
        released: list[str] = []
        for batch in waiting:
            if not batch_settled(essential_states(batch.stable_ids, self._src.facts_fn(batch.stable_ids))):
                continue
            self._covers.schedule(batch.stable_ids)
            with self._lock:
                batch.artwork = "queued"
                self._save()
            log.info(
                "ingest enrichment: batch %s essentials settled, %d cover lookups queued",
                batch.batch_id,
                len(batch.stable_ids),
            )
            released.append(batch.batch_id)
        return released

    def _record_cover(self, stable_id: str, outcome: Outcome) -> None:
        with self._lock:
            self._cover_outcomes[stable_id] = outcome

    def _log_new_warnings(self) -> list[EnrichWarning]:
        warnings = self._src.warnings_fn()
        for warning in warnings:
            if (warning.lane, warning.code) not in self._warned:
                self._warned.add((warning.lane, warning.code))
                log.warning(
                    "ingest enrichment: %s [%s] %s Fix: %s", warning.lane, warning.code, warning.message, warning.fix
                )
        return warnings

    # --- reads ------------------------------------------------------------------
    def warnings(self) -> list[dict[str, str]]:
        return [asdict(w) for w in self._log_new_warnings()]

    def status(self, batch_id: str | None = None, *, with_tracks: bool = False) -> dict[str, Any]:
        """Per batch: lane counts in priority order, artwork stage, and (optionally) per track."""
        with self._lock:
            batches = list(self._batches.values())
        if batch_id is not None:
            batches = [batch for batch in batches if batch.batch_id == batch_id]
            if not batches:
                raise KeyError(batch_id)
        return {
            "lane_priority": list(LANE_PRIORITY),
            "warnings": self.warnings(),
            "batches": [self._batch_status(batch, with_tracks) for batch in reversed(batches)],
        }

    def _batch_status(self, batch: Batch, with_tracks: bool) -> dict[str, Any]:
        rows: dict[str, dict[str, tuple[str, str | None]]] = {
            sid: {**lanes, ARTWORK_LANE: self._artwork_state(sid, batch)}
            for sid, lanes in essential_states(batch.stable_ids, self._src.facts_fn(batch.stable_ids)).items()
        }
        out: dict[str, Any] = {
            "batch_id": batch.batch_id,
            "source": batch.source,
            "registered_at": batch.registered_at,
            "tracks": len(batch.stable_ids),
            "artwork_stage": batch.artwork,
            "essentials_settled": batch_settled(
                {sid: {k: v for k, v in lanes.items() if k != ARTWORK_LANE} for sid, lanes in rows.items()}
            ),
            "lanes": lane_counts(rows, LANE_PRIORITY),
        }
        if with_tracks:
            out["track_lanes"] = {
                sid: {lane: {"state": state, "reason": reason} for lane, (state, reason) in lanes.items()}
                for sid, lanes in rows.items()
            }
        return out

    def _artwork_state(self, sid: str, batch: Batch) -> tuple[str, str | None]:
        if batch.artwork == "waiting":
            return "pending", "waits until every track in the batch has its playback essentials"
        outcome = self._cover_outcomes.get(sid)
        if outcome is None:
            return "pending", "queued for the online lookup"
        if outcome.status in ("has_cover", "found"):
            return "done", outcome.reason
        if outcome.status == "suggested":
            return "suggested", outcome.reason
        if outcome.status == "deferred":
            return "pending", outcome.reason
        if outcome.status == "skipped" and "ODJ_ARTWORK_ONLINE" in outcome.reason:
            return "unavailable", outcome.reason
        return "failed", outcome.reason

    # --- rerun --------------------------------------------------------------------
    def rerun(self, *, batch_id: str | None = None, playlist_id: str | None = None) -> dict[str, Any]:
        """Enrich a batch or a playlist again: forget its failures and cover misses, re-prioritise."""
        if (batch_id is None) == (playlist_id is None):
            raise ValueError("name exactly one of batch_id or playlist_id")
        if batch_id is not None:
            with self._lock:
                if batch_id not in self._batches:
                    raise KeyError(batch_id)
                ids = list(self._batches[batch_id].stable_ids)
            name, source = batch_id, "rerun"
        else:
            assert playlist_id is not None
            ids = self._src.playlist_ids_fn(playlist_id)
            name, source = f"playlist-{playlist_id}", "rerun-playlist"
        if self._src.retry_fn is not None:
            self._src.retry_fn(ids)
        self._src.forget_misses_fn(ids)
        with self._lock:
            for sid in ids:
                self._cover_outcomes.pop(sid, None)
        batch = self.register(name, ids, source)
        return {"ok": True, "batch_id": batch.batch_id, "tracks": len(ids)}

    # --- persistence ----------------------------------------------------------------
    def _load(self) -> None:
        path = self._src.store_path
        if path is None or not path.is_file():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._watermark = payload["watermark"]
        for raw in payload["batches"]:
            batch = Batch(**raw)
            batch.artwork = "waiting"  # outcomes are not persisted; a re-release reads the cache
            self._batches[batch.batch_id] = batch

    def _save(self) -> None:
        path = self._src.store_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"watermark": self._watermark, "batches": [asdict(b) for b in self._batches.values()]})
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, path)

    # --- loop -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        while not self._halt.is_set():
            self._wake.wait(TICK_INTERVAL_S)
            self._wake.clear()
            if self._halt.is_set():
                return
            try:
                self.tick()
            except Exception:
                log.exception("ingest enrichment tick failed")


# -----------------------------------------------------------------------------
# engine wiring: real sources
# -----------------------------------------------------------------------------
PLACEHOLDER_SQL = (
    "SELECT DISTINCT stable_id FROM analysis WHERE backend = ? AND backend_version = ? "
    "AND json_extract(record_json, '$.lanes.beatgrid.status') = 'ok' "
    "AND (COALESCE(json_extract(record_json, '$.lanes.beatgrid.payload.bpm_confidence'), 0) <= 0 "
    "OR COALESCE(json_extract(record_json, '$.lanes.beatgrid.payload.bpm'), 0) <= 0)"
)


def placeholder_grid_ids(conn_factory: Callable[[], sqlite3.Connection]) -> set[str]:
    """Current own beatgrid records whose grid is a placeholder (no BPM or zero confidence)."""
    from apps.webui.server.ahead_analysis_records import producer_version

    backend = LANE_BACKENDS["beatgrid"]
    conn = conn_factory()
    try:
        rows = conn.execute(PLACEHOLDER_SQL, (backend, producer_version(backend))).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return set()
        raise
    finally:
        conn.close()
    return {row[0] for row in rows}


def _in_clause(ids: Sequence[str]) -> str:
    return ",".join("?" * len(ids))


def present_ids(conn_factory: Callable[[], sqlite3.Connection], ids: Sequence[str]) -> set[str]:
    """Ids whose live row points at a file that exists here."""
    if not ids:
        return set()
    conn = conn_factory()
    try:
        rows = conn.execute(
            f"SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({_in_clause(ids)})",
            list(ids),
        ).fetchall()
    finally:
        conn.close()
    return {sid for sid, path in rows if path and Path(path).is_file()}


def new_track_rows(conn_factory: Callable[[], sqlite3.Connection], after_rowid: int) -> tuple[list[str], int]:
    conn = conn_factory()
    try:
        rows = conn.execute(
            "SELECT rowid, stable_id FROM tracks WHERE rowid > ? AND deleted_at IS NULL ORDER BY rowid",
            (after_rowid,),
        ).fetchall()
        top = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM tracks").fetchone()[0]
    finally:
        conn.close()
    return [row[1] for row in rows], int(top)


def playlist_member_ids(conn_factory: Callable[[], sqlite3.Connection], playlist_id: str) -> list[str]:
    conn = conn_factory()
    try:
        if (
            conn.execute(
                "SELECT 1 FROM playlists WHERE playlist_id = ? AND deleted_at IS NULL", (playlist_id,)
            ).fetchone()
            is None
        ):
            raise KeyError(playlist_id)
        rows = conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY position",
            (playlist_id,),
        ).fetchall()
    finally:
        conn.close()
    return list(dict.fromkeys(row[0] for row in rows))


def build_for_app(app: Any) -> EnrichmentCoordinator:
    from apps.analysis_waveform import local_waveform
    from apps.genre_infer import providers as genre_providers
    from apps.shared import artwork_sources
    from apps.shared.state import db as state_db_module
    from apps.shared.state.ingest import tag_refresh
    from apps.webui.server.ahead_analysis_records import declined_ids, done_ids
    from apps.webui.server.coverage_drain_analysis import DeckGate
    from apps.webui.server.rb_vendor_pkg.track_rows import bulk_rb_meta

    state_db = getattr(app.state, "state_db_path", None)

    def open_ro() -> sqlite3.Connection:
        return state_db_module.open_ro(None if state_db is None else Path(state_db))

    def drain() -> Any:
        return getattr(app.state, "ahead_analysis", None)

    def facts(ids: Sequence[str]) -> LaneFacts:
        running = drain()
        present = present_ids(open_ro, ids)
        conn = open_ro()
        try:
            blank = {row.stable_id for row in tag_refresh.blank_rows(conn, list(ids))}
        finally:
            conn.close()
        mapped = set(bulk_rb_meta(list(present))) if present else set()
        return LaneFacts(
            present=present,
            blank_tags=blank,
            mapped=mapped,
            has_strip={sid for sid in present - mapped if local_waveform.local_preview_strip(sid)[0] is not None},
            done={backend: done_ids(open_ro, backend) for backend in LANE_BACKENDS.values()},
            declined={lane: declined_ids(open_ro, lane, backend) for lane, backend in LANE_BACKENDS.items()},
            placeholder=placeholder_grid_ids(open_ro),
            failures=running.failures() if running is not None else {},
            unavailable=dict(running.status().unavailable) if running is not None else {},
            drain_running=running is not None,
        )

    def warnings() -> list[EnrichWarning]:
        running = drain()
        return capability_warnings(
            drain_running=running is not None,
            unavailable=dict(running.status().unavailable) if running is not None else {},
            artwork_online=artwork_sources.online_enabled(),
            ai_missing_keys=genre_providers.missing_keys(),
        )

    def forget_misses(ids: Sequence[str]) -> None:
        from apps.adapters.rekordbox import paths

        cache = paths.online_artwork_cache()
        for sid in ids:
            cache.forget_miss(sid)

    def prioritize(ids: Sequence[str]) -> None:
        running = drain()
        if running is not None:
            running.prioritize(ids)

    def retry(ids: Sequence[str]) -> None:
        running = drain()
        if running is not None:
            running.retry_failed_for(ids)

    return EnrichmentCoordinator(
        EnrichmentSources(
            facts_fn=facts,
            new_rows_fn=lambda mark: new_track_rows(open_ro, mark),
            playlist_ids_fn=lambda pid: playlist_member_ids(open_ro, pid),
            warnings_fn=warnings,
            playing_fn=DeckGate(lambda: getattr(app.state, "ui_mirror", None)),
            prioritize_fn=prioritize,
            retry_fn=retry,
            forget_misses_fn=forget_misses,
            # An app built with no state.db path (unit-test apps) keeps its batches in memory only.
            store_path=None if state_db is None else Path(state_db).parent / STORE_FILENAME,
        )
    )


def coordinator_for(app: Any) -> EnrichmentCoordinator:
    """The app's coordinator, built on first use; its loop runs only where the daemon armed it."""
    coordinator = getattr(app.state, "ingest_enrichment", None)
    if coordinator is None:
        coordinator = build_for_app(app)
        app.state.ingest_enrichment = coordinator
    return coordinator


__all__ = [
    "ESSENTIAL_LANES",
    "LANE_PRIORITY",
    "Batch",
    "EnrichWarning",
    "EnrichmentCoordinator",
    "EnrichmentSources",
    "LaneFacts",
    "batch_settled",
    "build_for_app",
    "capability_warnings",
    "coordinator_for",
    "essential_states",
]
