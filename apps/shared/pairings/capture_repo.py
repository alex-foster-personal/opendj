"""Durable CRUD for pairing sync snapshots and alignment marks."""
from __future__ import annotations

import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .schema_sql import apply_pairing_capture_migrations

MasterSide = Literal["a", "b"]
SyncMode = Literal["bar", "beat"]
AnchorKind = Literal["hotcue", "ms"]


class PairingCaptureError(ValueError):
    """Raised when a capture cannot satisfy its durable schema contract."""


@dataclass(frozen=True)
class SyncSnapshot:
    id: str
    stable_a: str
    stable_b: str
    master_side: MasterSide
    sync_mode: SyncMode
    a_tempo_ratio: float
    b_tempo_ratio: float
    a_position_beat_n: int | None
    a_position_phase: float | None
    a_position_ms: float
    b_position_beat_n: int | None
    b_position_phase: float | None
    b_position_ms: float
    captured_at: str


@dataclass(frozen=True)
class Alignment:
    id: str
    stable_a: str
    stable_b: str
    anchor_a_kind: AnchorKind
    anchor_b_kind: AnchorKind
    anchor_a_slot: str | None
    anchor_b_slot: str | None
    anchor_a_ms: float
    anchor_b_ms: float
    label: str | None
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_distinct_tracks(stable_a: str, stable_b: str) -> None:
    if not stable_a or not stable_b:
        raise PairingCaptureError("stable_a and stable_b are required")
    if stable_a == stable_b:
        raise PairingCaptureError("pairing capture requires two distinct tracks")


def _validate_choice(name: str, value: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise PairingCaptureError(f"{name} must be one of {choices}; got {value!r}")


def _validate_non_negative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise PairingCaptureError(f"{name} must be finite and non-negative")


def _snapshot_from_row(row: tuple[object, ...]) -> SyncSnapshot:
    return SyncSnapshot(*row)  # type: ignore[arg-type]


def _alignment_from_row(row: tuple[object, ...]) -> Alignment:
    return Alignment(*row)  # type: ignore[arg-type]


_SNAPSHOT_COLUMNS = (
    "id, stable_a, stable_b, master_side, sync_mode, a_tempo_ratio, b_tempo_ratio, "
    "a_position_beat_n, a_position_phase, a_position_ms, b_position_beat_n, "
    "b_position_phase, b_position_ms, captured_at"
)
_ALIGNMENT_COLUMNS = (
    "id, stable_a, stable_b, anchor_a_kind, anchor_b_kind, anchor_a_slot, "
    "anchor_b_slot, anchor_a_ms, anchor_b_ms, label, created_at"
)


class PairingCaptureRepo:
    """Repository over the migration-owned pairing capture tables."""

    def __init__(self, conn: sqlite3.Connection, *, ensure_schema: bool = True) -> None:
        self.conn = conn
        if ensure_schema:
            apply_pairing_capture_migrations(conn)

    def add_snapshot(  # noqa: PLR0913 - mirrors the persisted snapshot columns.
        self,
        *,
        stable_a: str,
        stable_b: str,
        master_side: MasterSide,
        sync_mode: SyncMode,
        a_tempo_ratio: float,
        b_tempo_ratio: float,
        a_position_ms: float,
        b_position_ms: float,
        a_position_beat_n: int | None = None,
        a_position_phase: float | None = None,
        b_position_beat_n: int | None = None,
        b_position_phase: float | None = None,
    ) -> SyncSnapshot:
        _validate_distinct_tracks(stable_a, stable_b)
        _validate_choice("master_side", master_side, ("a", "b"))
        _validate_choice("sync_mode", sync_mode, ("bar", "beat"))
        for name, value in (
            ("a_tempo_ratio", a_tempo_ratio),
            ("b_tempo_ratio", b_tempo_ratio),
            ("a_position_ms", a_position_ms),
            ("b_position_ms", b_position_ms),
        ):
            _validate_non_negative(name, value)
        row = SyncSnapshot(
            id=str(uuid.uuid4()), stable_a=stable_a, stable_b=stable_b,
            master_side=master_side, sync_mode=sync_mode,
            a_tempo_ratio=a_tempo_ratio, b_tempo_ratio=b_tempo_ratio,
            a_position_beat_n=a_position_beat_n, a_position_phase=a_position_phase,
            a_position_ms=a_position_ms, b_position_beat_n=b_position_beat_n,
            b_position_phase=b_position_phase, b_position_ms=b_position_ms,
            captured_at=_now(),
        )
        self.conn.execute(
            f"INSERT INTO pairing_sync_snapshots ({_SNAPSHOT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(row.__dict__.values()),
        )
        return row

    def get_snapshot(self, snapshot_id: str) -> SyncSnapshot | None:
        row = self.conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM pairing_sync_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def list_snapshots(
        self, *, stable_a: str | None = None, stable_b: str | None = None, limit: int = 50
    ) -> list[SyncSnapshot]:
        if limit < 1:
            raise PairingCaptureError("limit must be positive")
        clauses: list[str] = []
        values: list[object] = []
        if stable_a is not None:
            clauses.append("stable_a = ?")
            values.append(stable_a)
        if stable_b is not None:
            clauses.append("stable_b = ?")
            values.append(stable_b)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM pairing_sync_snapshots{where} "
            "ORDER BY captured_at DESC, id DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [_snapshot_from_row(row) for row in rows]

    def add_alignment(  # noqa: PLR0913 - mirrors the persisted alignment columns.
        self,
        *,
        stable_a: str,
        stable_b: str,
        anchor_a_kind: AnchorKind,
        anchor_b_kind: AnchorKind,
        anchor_a_ms: float,
        anchor_b_ms: float,
        anchor_a_slot: str | None = None,
        anchor_b_slot: str | None = None,
        label: str | None = None,
    ) -> Alignment:
        _validate_distinct_tracks(stable_a, stable_b)
        _validate_choice("anchor_a_kind", anchor_a_kind, ("hotcue", "ms"))
        _validate_choice("anchor_b_kind", anchor_b_kind, ("hotcue", "ms"))
        _validate_non_negative("anchor_a_ms", anchor_a_ms)
        _validate_non_negative("anchor_b_ms", anchor_b_ms)
        row = Alignment(
            id=str(uuid.uuid4()), stable_a=stable_a, stable_b=stable_b,
            anchor_a_kind=anchor_a_kind, anchor_b_kind=anchor_b_kind,
            anchor_a_slot=anchor_a_slot, anchor_b_slot=anchor_b_slot,
            anchor_a_ms=anchor_a_ms, anchor_b_ms=anchor_b_ms, label=label,
            created_at=_now(),
        )
        self.conn.execute(
            f"INSERT INTO pairing_alignments ({_ALIGNMENT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(row.__dict__.values()),
        )
        return row

    def list_alignments(
        self, *, stable_a: str | None = None, stable_b: str | None = None
    ) -> list[Alignment]:
        clauses: list[str] = []
        values: list[object] = []
        if stable_a is not None and stable_b is not None:
            clauses.append("((stable_a = ? AND stable_b = ?) OR (stable_a = ? AND stable_b = ?))")
            values.extend((stable_a, stable_b, stable_b, stable_a))
        elif stable_a is not None:
            clauses.append("stable_a = ? OR stable_b = ?")
            values.extend((stable_a, stable_a))
        elif stable_b is not None:
            clauses.append("stable_a = ? OR stable_b = ?")
            values.extend((stable_b, stable_b))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT {_ALIGNMENT_COLUMNS} FROM pairing_alignments{where} "
            "ORDER BY created_at DESC, id DESC",
            values,
        ).fetchall()
        return [_alignment_from_row(row) for row in rows]


__all__ = ["Alignment", "PairingCaptureError", "PairingCaptureRepo", "SyncSnapshot"]
