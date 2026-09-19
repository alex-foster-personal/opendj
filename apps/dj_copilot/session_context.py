"""Session context loader (AI-01, Phase 13 Plan 03).

A ``SessionContext`` is the last N played tracks plus a provenance tag.
The loader picks the freshest available source via a documented
precedence:

  auto  -> phase12 > rekordbox_history > djay_history > empty
  manual -> pre-built list (tests / CLI --session-json)

Phase 12 + Rekordbox + djay are stub-policy wired. When the source is
not yet available, ``load_session_context`` falls through gracefully
and returns the next source's payload (or an empty context).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PlayedTrack:
    """One row in the recent-plays tail."""

    stable_id: str
    artist: str | None
    bpm: float | None
    key_camelot: str | None
    energy: int | None
    played_at: datetime
    tags: tuple[str, ...] = ()


Source = Literal[
    "phase12",
    "rekordbox_history",
    "djay_history",
    "manual",
    "empty",
    "auto",
]


@dataclass(slots=True)
class SessionContext:
    recent: list[PlayedTrack]
    source: Source
    captured_at: datetime


def _load_phase12(
    conn: sqlite3.Connection, limit: int
) -> list[PlayedTrack]:
    try:
        rows = conn.execute(
            "SELECT stable_id, artist, bpm, key_camelot, energy, played_at "
            "FROM session_events "
            "WHERE action='now_playing' "
            "ORDER BY played_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[PlayedTrack] = []
    for r in rows:
        played_at = (
            datetime.fromisoformat(r[5])
            if isinstance(r[5], str)
            else datetime.now(UTC)
        )
        out.append(
            PlayedTrack(
                stable_id=r[0],
                artist=r[1],
                bpm=r[2],
                key_camelot=r[3],
                energy=r[4],
                played_at=played_at,
            )
        )
    out.reverse()  # newest last, per dataclass contract.
    return out


def _load_empty() -> list[PlayedTrack]:
    return []


def load_session_context(
    *,
    conn: sqlite3.Connection | None = None,
    source: Source = "auto",
    limit: int = 12,
    manual: list[PlayedTrack] | None = None,
) -> SessionContext:
    """Best-effort loader. Never raises.

    ``source="auto"`` tries phase12 -> rekordbox_history -> djay_history
    -> empty in order. Phase 13 wires phase12 + empty; the vendor
    history adapters (rekordbox_history + djay_history) are stubbed --
    they return empty lists and log at INFO the first time a fallback
    happens.
    """
    now = datetime.now(UTC)

    if source == "manual":
        return SessionContext(
            recent=list(manual or []), source="manual", captured_at=now
        )

    tried: list[Source] = (
        ["phase12", "rekordbox_history", "djay_history"]
        if source == "auto"
        else [source]
    )
    for s in tried:
        if s == "phase12" and conn is not None:
            rows = _load_phase12(conn, limit)
            if rows:
                return SessionContext(recent=rows, source="phase12", captured_at=now)
        elif s == "rekordbox_history":
            # Stub: Phase 12 + RB history adapter tbd. Return empty so
            # the auto fallback proceeds.
            _logger.info(
                "rekordbox_history adapter not yet wired; returning empty"
            )
        elif s == "djay_history":
            _logger.info("djay_history adapter not yet wired; returning empty")

    # P13-F02: when the caller explicitly requested a non-auto source, do
    # not silently relabel the empty result as "empty" -- that made an
    # intentional phase12/rekordbox_history/djay_history request
    # indistinguishable from a fall-through. Preserve the requested label
    # so callers (and test harnesses) can see the request was honored but
    # yielded no rows.
    if source != "auto":
        return SessionContext(
            recent=_load_empty(), source=source, captured_at=now
        )
    return SessionContext(recent=_load_empty(), source="empty", captured_at=now)
