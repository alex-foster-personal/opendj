"""Migration 27 -> 28: AutoPlay decision record and one-tap verdict (ADR-NNNN).

``pair_decisions`` keeps the lookahead's last ``Decision`` per ordered pair (shortlist, windows shown, rejections
with reasons, every candidate's style and rule score, the chosen style and bars), so a post-mortem can recover why a
track won. It sits beside ``pair_transitions`` rather than inside it: a decision is made before anything plays, and
``pair_transitions`` gets its row only when the transition runs. ``pair_verdicts`` is the listener's one-tap
good / meh / bad on the transition that ran, one row per pair beside its ``pair_transitions`` row: a new table, not
columns added to that one, because every store adopts tables by shape and an ``ALTER TABLE`` on an existing table
breaks that audit on every older database. Pure logging: the planner never reads either.

Machine-local like ``pair_transitions``: no write-token trigger and no wire change.
"""

from __future__ import annotations

PAIR_VERDICTS: tuple[str, ...] = ("good", "meh", "bad")

_V28: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS pair_decisions (
        out_track_id TEXT NOT NULL,
        in_track_id TEXT NOT NULL,
        decision_json TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        PRIMARY KEY (out_track_id, in_track_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pair_decisions_decided_at ON pair_decisions (decided_at)",
    """
    CREATE TABLE IF NOT EXISTS pair_verdicts (
        out_track_id TEXT NOT NULL,
        in_track_id TEXT NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('good', 'meh', 'bad')),
        verdict_at TEXT NOT NULL,
        PRIMARY KEY (out_track_id, in_track_id)
    )
    """,
]

__all__ = ["PAIR_VERDICTS", "_V28"]
