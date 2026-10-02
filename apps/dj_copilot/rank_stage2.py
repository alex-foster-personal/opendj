"""Stage 2 ranker: Phase 8 pairings re-rank (AI-01)."""
from __future__ import annotations

import logging
import sqlite3

from .rank_stage1 import ScoredCandidate

_logger = logging.getLogger(__name__)
_warned_missing_pairings = False

# Score bumps per pair source (Plan 03 §4.1).
_BUMPS: dict[str, float] = {
    "manual": 0.15,
    "learned": 0.05,
    "ai": 0.02,
}


def _pairings_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='pairings'"
    ).fetchone()
    return row is not None


def rerank_with_pairings(
    *,
    conn: sqlite3.Connection,
    current_stable_id: str,
    stage1: list[ScoredCandidate],
) -> list[ScoredCandidate]:
    """Bump candidates the pairings table says follow ``current_stable_id``.

    A row pairs ``current -> candidate`` when it is ``(current, candidate)``
    with direction ``into`` or ``either``, or ``(candidate, current)`` with
    ``out_of`` ("paired when candidate's partner is current") or ``either``
    (undirected, stored either way round). When the ``pairings``
    table does not exist, the stage is a no-op and logs a one-time
    INFO message.
    """
    global _warned_missing_pairings
    if not _pairings_table_exists(conn):
        if not _warned_missing_pairings:
            _logger.info(
                "pairings table missing; Stage 2 rerank is a no-op until "
                "Phase 8 ships."
            )
            _warned_missing_pairings = True
        return stage1

    rows = conn.execute(
        "SELECT to_stable_id, source FROM pairings "
        "WHERE from_stable_id=? AND direction IN ('into','either') "
        "UNION ALL "
        "SELECT from_stable_id, source FROM pairings "
        "WHERE to_stable_id=? AND direction IN ('out_of','either')",
        (current_stable_id, current_stable_id),
    ).fetchall()
    pair_index: dict[str, str] = {r[0]: r[1] for r in rows}
    if not pair_index:
        return stage1

    out: list[ScoredCandidate] = []
    for sc in stage1:
        src = pair_index.get(sc.stable_id)
        if src:
            bump = _BUMPS.get(src, 0.0)
            new_score = sc.score + bump
            rationale = dict(sc.rationale)
            rationale[f"pair_{src}"] = 1.0
            out.append(
                ScoredCandidate(
                    stable_id=sc.stable_id,
                    score=round(new_score, 6),
                    rationale=rationale,
                    candidate=sc.candidate,
                )
            )
        else:
            out.append(sc)
    # Re-sort deterministically after the bump.
    out.sort(key=lambda s: (-s.score, s.stable_id))
    return out
