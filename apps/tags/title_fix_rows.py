"""Row types of the title-fix sidecar store and the mappers from sqlite rows (split out of title_fix_store)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class ProposalRow:
    proposal_id: str
    scan_id: str
    stable_id: str
    created_at: str
    original_title: str
    original_artist: str
    original_artists: list[str]
    proposed_title: str
    proposed_artist: str
    original_artist_credit: str
    tags: list[str]
    fixer_status: str
    source: str
    confidence: float | None
    jev_kind: str
    notes: list[str]
    error: str
    cost_usd: float
    state: str
    batch_id: str | None
    tags_added: list[str] = field(default_factory=list)
    decided_at: str | None = None

    @property
    def changes_title(self) -> bool:
        return self.proposed_title != self.original_title

    @property
    def changes_artist(self) -> bool:
        return self.proposed_artist != self.original_artist

    @property
    def producer_credit_removed(self) -> bool:
        return any(note.startswith(PRODUCER_NOTE_PREFIX) for note in self.notes)


@dataclass(frozen=True)
class ScanRow:
    scan_id: str
    started_at: str
    finished_at: str | None
    state: str
    requested: int
    processed: int
    cost_usd: float
    spend_cap_usd: float
    error: str | None
    auto_batch_id: str | None
    auto_apply_policy: str


@dataclass(frozen=True)
class BatchRow:
    batch_id: str
    created_at: str
    kind: str
    policy: str
    proposal_count: int
    undone_at: str | None
    committed_at: str | None
    undo_started_at: str | None
    undo_targets_json: str | None = None
    applied_state_json: str | None = None

    @property
    def applied_state(self) -> dict[str, dict[str, Any]]:
        """proposal_id -> {"etag", "tags"}: the track's ETag and complete tag set as of this batch's committed write,
        read inside the write transaction. Empty when none was recorded (a crash between the write and its commit
        marker, or a batch that predates the record)."""
        return {} if self.applied_state_json is None else json.loads(self.applied_state_json)

    @property
    def undo_targets(self) -> list[str] | None:
        """The proposal ids the running undo restores; None when no undo runs (or it predates this record)."""
        return None if self.undo_targets_json is None else json.loads(self.undo_targets_json)


def _proposal_from_row(row: sqlite3.Row) -> ProposalRow:
    return ProposalRow(
        proposal_id=row["proposal_id"],
        scan_id=row["scan_id"],
        stable_id=row["stable_id"],
        created_at=row["created_at"],
        original_title=row["original_title"],
        original_artist=row["original_artist"],
        original_artists=json.loads(row["original_artists_json"]),
        proposed_title=row["proposed_title"],
        proposed_artist=row["proposed_artist"],
        original_artist_credit=row["original_artist_credit"],
        tags=json.loads(row["tags_json"]),
        fixer_status=row["fixer_status"],
        source=row["source"],
        confidence=row["confidence"],
        jev_kind=row["jev_kind"],
        notes=json.loads(row["notes_json"]),
        error=row["error"],
        cost_usd=row["cost_usd"],
        state=row["state"],
        batch_id=row["batch_id"],
        tags_added=json.loads(row["tags_added_json"]),
        decided_at=row["decided_at"],
    )


def _scan_from_row(row: sqlite3.Row) -> ScanRow:
    return ScanRow(**{f.name: row[f.name] for f in fields(ScanRow)})


_BATCH_FIELDS = tuple(f.name for f in fields(BatchRow))


def _batch_from_row(row: sqlite3.Row) -> BatchRow:
    """Only the columns BatchRow knows: a store written by an earlier build may carry a renamed legacy column."""
    return BatchRow(**{name: row[name] for name in _BATCH_FIELDS})


@dataclass(frozen=True)
class ProposalFilter:
    state: str | None = "pending"
    fixer_status: str | None = None
    source: str | None = None
    tag: str | None = None
    scan_id: str | None = None
    batch_id: str | None = None
    limit: int = 100
    offset: int = 0


PRODUCER_NOTE_PREFIX = "producer credit removed from the title:"
