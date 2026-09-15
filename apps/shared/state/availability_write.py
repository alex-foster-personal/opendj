"""Shared ``track_availability`` upsert primitive.

Both the MIK CLI and the engine availability worker route writes through
this module so there is exactly one SQL path. ``StateWriter`` wraps it with
durable events; callers that only need the table update (dry-run guards)
import from here directly.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from apps.shared.scan_mass_missing import guard_scan_count
from apps.shared.state.locations import ID_BIND_BATCH
from apps.shared.state.schema import AVAILABILITY_STATES


@dataclass(frozen=True)
class AvailabilityRow:
    stable_id: str
    state: str
    checked_path: str | None


@dataclass
class AvailabilityReport:
    counts: dict[str, int] = field(default_factory=dict)
    changed: int = 0
    unchanged: int = 0
    total: int = 0

    def bump(self, state: str) -> None:
        self.counts[state] = self.counts.get(state, 0) + 1


@dataclass
class AvailabilityWriteReport:
    counts: dict[str, int] = field(default_factory=dict)
    changed: int = 0
    unchanged: int = 0
    total: int = 0
    changed_stable_ids: list[str] = field(default_factory=list)

    def bump(self, state: str) -> None:
        self.counts[state] = self.counts.get(state, 0) + 1


def present_count(conn: sqlite3.Connection) -> int:
    """Live tracks currently classified as ``present``."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM track_availability "
            "JOIN tracks ON tracks.stable_id = track_availability.stable_id "
            "WHERE tracks.deleted_at IS NULL AND track_availability.state = 'present'"
        ).fetchone()[0]
    )


def projected_present_count(
    conn: sqlite3.Connection,
    rows: list[AvailabilityRow],
) -> int:
    """Present count after applying ``rows`` without writing them."""
    present = {
        stable_id
        for stable_id, in conn.execute(
            "SELECT track_availability.stable_id FROM track_availability "
            "JOIN tracks ON tracks.stable_id = track_availability.stable_id "
            "WHERE tracks.deleted_at IS NULL AND track_availability.state = 'present'"
        )
    }
    for row in rows:
        if row.state == "present":
            present.add(row.stable_id)
        else:
            present.discard(row.stable_id)
    return len(present)


def guard_present_drop(
    conn: sqlite3.Connection,
    rows: list[AvailabilityRow],
    *,
    allow_mass_missing: bool = False,
) -> None:
    """Refuse a probe that would wipe a previously present library (LIBM-41)."""
    prior = present_count(conn)
    current = sum(1 for row in rows if row.state == "present")
    guard_scan_count(
        "library",
        current,
        prior if prior else None,
        allow_mass_missing=allow_mass_missing,
    )


def guard_round_present_drop(
    conn: sqlite3.Connection,
    rows: list[AvailabilityRow],
    *,
    round_start_present: int,
    allow_mass_missing: bool = False,
) -> None:
    """Refuse a background batch that collapses the trusted present count."""
    if round_start_present <= 0:
        return
    projected = projected_present_count(conn, rows)
    guard_scan_count(
        "library-round",
        projected,
        round_start_present,
        allow_mass_missing=allow_mass_missing,
    )


def _load_existing_availability(
    conn: sqlite3.Connection,
    existing_scope_stable_ids: Sequence[str] | None,
) -> dict[str, tuple[str, str | None]]:
    if existing_scope_stable_ids is None:
        return {
            stable_id: (state, checked_path)
            for stable_id, state, checked_path in conn.execute(
                "SELECT stable_id, state, checked_path FROM track_availability"
            )
        }
    existing: dict[str, tuple[str, str | None]] = {}
    scope = list(existing_scope_stable_ids)
    for start in range(0, len(scope), ID_BIND_BATCH):
        chunk = scope[start : start + ID_BIND_BATCH]
        placeholders = ",".join("?" * len(chunk))
        for stable_id, state, checked_path in conn.execute(
            f"SELECT stable_id, state, checked_path FROM track_availability "
            f"WHERE stable_id IN ({placeholders})",
            chunk,
        ):
            existing[stable_id] = (state, checked_path)
    return existing


def upsert_availability_rows(
    conn: sqlite3.Connection,
    rows: list[AvailabilityRow],
    *,
    now: str | None = None,
    allow_mass_missing: bool = False,
    apply_mass_missing_guard: bool = True,
    always_refresh_checked_at: bool = False,
    existing_scope_stable_ids: Sequence[str] | None = None,
) -> AvailabilityWriteReport:
    """Upsert ``rows`` into ``track_availability``. Idempotent.

    ``apply_mass_missing_guard=False`` is for engine-sized partial batches that
    must not compare a local batch count with the whole library.

    ``always_refresh_checked_at=False`` (the default) preserves the MIK CLI's
    documented semantics: ``checked_at`` records the last STATE CHANGE, and a
    row whose ``(state, checked_path)`` comes back identical to what is
    already stored is a pure no-op. The engine's background worker opts in
    with ``True`` instead: it re-selects a row as soon as
    ``tracks.updated_at > track_availability.checked_at``, so a row whose
    classification is re-probed and comes back unchanged must still stamp
    ``checked_at`` forward, or it is re-selected as stale on every future
    pass forever (LIBM-41 follow-up). Stamping it forward is still counted
    as ``unchanged``, not ``changed`` -- the visible classification did not
    move, only the bookkeeping of when it was last confirmed.
    """
    if apply_mass_missing_guard:
        guard_present_drop(conn, rows, allow_mass_missing=allow_mass_missing)
    stamp = now or datetime.now(UTC).isoformat()
    report = AvailabilityWriteReport()
    existing = _load_existing_availability(conn, existing_scope_stable_ids)
    for row in rows:
        if row.state not in AVAILABILITY_STATES:
            raise ValueError(
                f"state {row.state!r} not in {AVAILABILITY_STATES}"
            )
        report.total += 1
        report.bump(row.state)
        prior = existing.get(row.stable_id)
        if prior == (row.state, row.checked_path):
            report.unchanged += 1
            if not always_refresh_checked_at:
                continue
        else:
            report.changed += 1
            report.changed_stable_ids.append(row.stable_id)
        conn.execute(
            "INSERT INTO track_availability(stable_id, state, checked_path, "
            "checked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(stable_id) DO UPDATE SET state=excluded.state, "
            "checked_path=excluded.checked_path, checked_at=excluded.checked_at",
            (row.stable_id, row.state, row.checked_path, stamp),
        )
    return report


__all__ = [
    "AvailabilityReport",
    "AvailabilityRow",
    "AvailabilityWriteReport",
    "guard_present_drop",
    "guard_round_present_drop",
    "present_count",
    "projected_present_count",
    "upsert_availability_rows",
]
