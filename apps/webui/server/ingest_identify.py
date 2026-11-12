"""Fingerprint identify lane worker (issue #10429)."""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.dedup.http import resolve_dedup_db
from apps.shared.fingerprints import ChromaprintMissing, FingerprintCache, FingerprintFailed, load_or_compute
from apps.tags.http import (
    IdentifyDeferred,
    IdentifyError,
    IdentifyItem,
    IdentifyProposal,
    ProposalStore,
    StorePaths,
)
from apps.tags.http import identify as identify_mod
from apps.webui.server.title_fix_core import _artist_flat

log = logging.getLogger(__name__)

QUEUE_MAX = 5000
DEFERRED_REASON = "AcoustID / MusicBrainz busy (HTTP 503); press Retry to look it up again"
KEY_FIX = "set ODJ_ACOUSTID_KEY (free key at acoustid.org)"

IdentifyOutcomeState = Literal[
    "proposed",
    "needs_review",
    "no_match",
    "has_artist",
    "deferred",
    "failed",
    "unavailable",
]


@dataclass(frozen=True)
class IdentifyOutcome:
    state: IdentifyOutcomeState
    reason: str | None = None


@dataclass(frozen=True)
class TrackRow:
    stable_id: str
    title: str
    artist_flat: str
    artists: list[str]
    file_path: str


@dataclass
class _RowRun:
    outcome: IdentifyOutcome
    item: IdentifyItem | None = None
    stop_batch: bool = False


def _identify_row(
    sid: str,
    row: TrackRow | None,
    *,
    key: str | None,
    fingerprint_fn: Callable[[str], tuple[str, float]],
    identify_fn: Callable[[str, float], IdentifyProposal | None],
) -> _RowRun:
    if row is None:
        return _RowRun(IdentifyOutcome("failed", "track not in library"))
    if row.artist_flat.strip():
        return _RowRun(IdentifyOutcome("has_artist", None))
    if key is None:
        return _RowRun(IdentifyOutcome("unavailable", KEY_FIX))
    try:
        fp, duration = fingerprint_fn(row.file_path)
    except (ChromaprintMissing, FingerprintFailed, FileNotFoundError) as exc:
        return _RowRun(IdentifyOutcome("failed", f"{type(exc).__name__}: {exc}"))
    try:
        proposal = identify_fn(fp, duration)
    except IdentifyDeferred:
        return _RowRun(IdentifyOutcome("deferred", DEFERRED_REASON), stop_batch=True)
    except IdentifyError as exc:
        return _RowRun(IdentifyOutcome("failed", str(exc)))
    if proposal is None:
        return _RowRun(IdentifyOutcome("no_match", None))
    state: IdentifyOutcomeState = "proposed" if proposal.status == "confident" else "needs_review"
    item = IdentifyItem(
        stable_id=sid,
        original_title=row.title,
        original_artist=row.artist_flat,
        original_artists=row.artists,
        proposal=proposal,
    )
    return _RowRun(
        IdentifyOutcome(state, f"proposal for {proposal.best.artist} - {proposal.best.title}"),
        item=item,
    )


def _record_identify_items(
    items: Sequence[IdentifyItem],
    outcomes: dict[str, IdentifyOutcome],
    record_fn: Callable[[Sequence[IdentifyItem]], object],
) -> None:
    try:
        record_fn(items)
    except Exception as exc:  # noqa: BLE001 - per-row failed outcomes, batch still settles
        msg = f"record_fn: {type(exc).__name__}: {exc}"
        for item in items:
            prior = outcomes.get(item.stable_id)
            if prior is not None and prior.state in ("proposed", "needs_review"):
                outcomes[item.stable_id] = IdentifyOutcome("failed", msg)


def run_identify(
    ids: Sequence[str],
    *,
    rows_fn: Callable[[Sequence[str]], dict[str, TrackRow]],
    fingerprint_fn: Callable[[str], tuple[str, float]],
    identify_fn: Callable[[str, float], IdentifyProposal | None],
    record_fn: Callable[[Sequence[IdentifyItem]], object],
    key_fn: Callable[[], str | None],
) -> dict[str, IdentifyOutcome]:
    rows = rows_fn(ids)
    outcomes: dict[str, IdentifyOutcome] = {}
    items: list[IdentifyItem] = []
    key = key_fn()
    stop_batch = False
    for sid in ids:
        if stop_batch:
            outcomes[sid] = IdentifyOutcome("deferred", DEFERRED_REASON)
            continue
        try:
            run = _identify_row(
                sid,
                rows.get(sid),
                key=key,
                fingerprint_fn=fingerprint_fn,
                identify_fn=identify_fn,
            )
        except Exception as exc:  # noqa: BLE001 - per-row failed outcomes, batch still settles
            outcomes[sid] = IdentifyOutcome("failed", f"{type(exc).__name__}: {exc}")
            continue
        outcomes[sid] = run.outcome
        if run.item is not None:
            items.append(run.item)
        if run.stop_batch:
            stop_batch = True
    if items:
        _record_identify_items(items, outcomes, record_fn)
    return outcomes


class IdentifyWorker:
    def __init__(
        self,
        run_fn: Callable[[Sequence[str]], Mapping[str, IdentifyOutcome]],
        on_outcomes: Callable[[str, Mapping[str, IdentifyOutcome]], None] | None = None,
        maxsize: int = QUEUE_MAX,
    ) -> None:
        self._run_fn = run_fn
        self._on_outcome = on_outcomes
        self._queue: queue.Queue[tuple[str, list[str]]] = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._start_guard = threading.Lock()

    def schedule(self, batch_id: str, stable_ids: Sequence[str]) -> int:
        ids = [sid for sid in stable_ids]
        if not ids:
            return 0
        try:
            self._queue.put_nowait((batch_id, ids))
        except queue.Full:
            log.warning("identify backfill: queue full (%d), batch %s not queued", self._queue.maxsize, batch_id)
            return 0
        self._ensure_worker()
        return len(ids)

    def join(self) -> None:
        self._queue.join()

    def _ensure_worker(self) -> None:
        with self._start_guard:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._drain, name="webui.ingest-identify", daemon=True)
                self._thread.start()

    def _drain(self) -> None:
        while True:
            batch_id, ids = self._queue.get()
            try:
                try:
                    outcomes = self._run_fn(ids)
                except Exception:
                    log.exception("identify worker failed for batch %s", batch_id)
                    outcomes = {sid: IdentifyOutcome("failed", "identify worker error") for sid in ids}
                if self._on_outcome is not None:
                    self._on_outcome(batch_id, outcomes)
            finally:
                self._queue.task_done()


def build_identify_fn(state_db: Path) -> Callable[[Sequence[str]], dict[str, IdentifyOutcome]]:
    from apps.shared.state import db as state_db_module

    cache = FingerprintCache(resolve_dedup_db(None))
    store = ProposalStore(StorePaths(state_db))

    def rows_fn(sids: Sequence[str]) -> dict[str, TrackRow]:
        if not sids:
            return {}
        conn = state_db_module.open_ro(state_db)
        try:
            marks = ",".join("?" * len(sids))
            fetched = conn.execute(
                f"SELECT stable_id, title, artists_json, file_path FROM tracks "
                f"WHERE deleted_at IS NULL AND stable_id IN ({marks})",
                list(sids),
            ).fetchall()
        finally:
            conn.close()
        out: dict[str, TrackRow] = {}
        for stable_id, title, artists_json, file_path in fetched:
            artists = json.loads(artists_json) if artists_json else []
            flat = _artist_flat(", ".join(artists) if artists else "")
            out[stable_id] = TrackRow(
                stable_id=stable_id,
                title=title or "",
                artist_flat=flat,
                artists=list(artists),
                file_path=file_path or "",
            )
        return out

    def fingerprint_fn(path: str) -> tuple[str, float]:
        fp = load_or_compute(Path(path), cache)
        return fp.fp_str, fp.duration

    def record_fn(items: Sequence[IdentifyItem]) -> None:
        store.add_identify_proposals(items)

    def identify_fn(fp: str, duration: float) -> IdentifyProposal | None:
        return identify_mod.identify(fp, duration)

    return lambda ids: run_identify(
        ids,
        rows_fn=rows_fn,
        fingerprint_fn=fingerprint_fn,
        identify_fn=identify_fn,
        record_fn=record_fn,
        key_fn=identify_mod.api_key,
    )
