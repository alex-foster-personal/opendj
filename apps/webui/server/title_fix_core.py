"""Shared types and store-contention helpers of the title-fix service (split out of title_fix_service)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.tags.http import (
    ProposalRow,
    ProposalStore,
    StorePaths,
)

from .backend import (
    ArtistsDataError,
    StateBackend,
    artists_from_json,
)


class CFG:
    BATCH_GRACE_S = 120  # an apply younger than this may still be between its track write and its commit stamp
    CHUNK = 16  # rows per fix_tracks call; the spend cap is checked between chunks
    BEATS_PER_STALE_WINDOW = 3  # a healthy heartbeat beats this many times per SCAN_STALE_S
    CONTENTION_RETRIES_PER_STALE_WINDOW = 30  # a beat that met store contention retries this often, not a full interval later
    STORE_BUSY_WAIT_S = 120.0  # how long a scan waits out a busy sidecar store before giving up on a write
    STORE_BUSY_PAUSE_S = 1.0
    CAS_REPLAN_ROUNDS = 3  # a write that loses its compare-and-swap re-plans without the raced rows, at most this often
    SCAN_STALE_S = 900  # a running scan whose worker has not progressed for this long is dead (a chunk is bounded well under it)
    WRITE_SOURCE = "open-dj-tool"
    MAX_SELECTION = 1000


class TitleFixError(Exception):
    """A refusal with a stable code; the route maps it onto the error envelope."""

    def __init__(self, code: str, message: str, status: int = 409, extra: dict | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra or {}


@dataclass(frozen=True)
class Ctx:
    paths: StorePaths
    store: ProposalStore
    backend: StateBackend
    write_gate: Callable[[], None] | None = None  # re-checks the live cloud lock; raises when this host may not write

    @classmethod
    def open(cls, state_db_path: Path, backend: StateBackend, write_gate: Callable[[], None] | None = None) -> Ctx:
        paths = StorePaths(Path(state_db_path))
        return cls(paths, ProposalStore(paths), backend, write_gate)


@dataclass
class Outcome:
    batch_id: str | None = None
    applied: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    warning: str | None = None  # the write committed and is undoable, but its result could not be fully confirmed


def _is_store_contention(exc: sqlite3.OperationalError) -> bool:
    """SQLITE_BUSY / SQLITE_LOCKED only: another writer holds the store, which clears by itself."""
    return getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _when_store_free(write: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """A scan's own store write, waited out while a manual apply holds the sidecar lock (SQLITE_BUSY / LOCKED) for up to
    STORE_BUSY_WAIT_S, so an unrelated apply cannot waste a paid chunk or strand the scan row as running. Any other error,
    or contention that outlasts the wait, raises."""
    give_up_at = time.monotonic() + CFG.STORE_BUSY_WAIT_S
    while True:
        try:
            return write(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if not _is_store_contention(exc) or time.monotonic() >= give_up_at:
                raise
            time.sleep(CFG.STORE_BUSY_PAUSE_S)


_ALL_ROWS = 1_000_000  # a batch is undone whole, however large its auto-apply was


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _title_text(value: str | None) -> str:
    """A missing title (NULL, as Rekordbox ingestion stores it) and an empty one are the same blank state, and the
    scan records that state as \"\". Comparisons normalize to it; an undo restores it as an empty string."""
    return value or ""


def _artist_flat(value: str | None) -> str:
    return value or ""


def _get_or_404(ctx: Ctx, proposal_ids: list[str]) -> list[ProposalRow]:
    try:
        return ctx.store.get_proposals(proposal_ids)
    except KeyError as exc:
        raise TitleFixError("title_fix_proposal_not_found", str(exc), 404) from exc


def _artists_or_refuse(stable_id: str, artists_json: str | None) -> list[str]:
    """The track's exact artists list, or a refusal naming the row whose storage is corrupt (never read as blank)."""
    try:
        return artists_from_json(artists_json)
    except ArtistsDataError as exc:
        raise TitleFixError(
            "title_fix_artists_corrupt", f"track {stable_id} has unreadable artists_json ({exc}); repair the row first", 422,
        ) from exc


UNVERIFIED = "unverified"  # key in a recovered batch's applied_state: its apply-time baseline died with the process


def _written_artists(row: ProposalRow) -> list[str]:
    return [row.proposed_artist] if row.proposed_artist else []
