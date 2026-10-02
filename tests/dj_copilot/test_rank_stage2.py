"""Stage 2 (pairings) re-rank tests (AI-01)."""
from __future__ import annotations

import sqlite3

import pytest

from apps.dj_copilot.rank_stage1 import ScoredCandidate
from apps.dj_copilot.rank_stage2 import rerank_with_pairings
from apps.shared.harmonic import TrackFeature

pytestmark = pytest.mark.requirement("AI-01")


def _seed_pairings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE pairings (
            from_stable_id TEXT NOT NULL,
            to_stable_id   TEXT NOT NULL,
            direction      TEXT NOT NULL,
            source         TEXT NOT NULL,
            notes          TEXT,
            confidence     REAL,
            created_at     TEXT NOT NULL,
            modified_at    TEXT NOT NULL
        )
        """
    )


def _sc(stable_id: str, score: float) -> ScoredCandidate:
    return ScoredCandidate(
        stable_id=stable_id,
        score=score,
        rationale={"camelot": 0.8, "bpm": 0.8, "energy": 0.8},
        candidate=TrackFeature(stable_id, None, 120.0, "8A", 5),
    )


def test_no_pairings_table_is_noop(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        stage1 = [_sc("a", 0.6), _sc("b", 0.5)]
        out = rerank_with_pairings(
            conn=conn, current_stable_id="cur", stage1=stage1
        )
        assert [s.stable_id for s in out] == ["a", "b"]
    finally:
        conn.close()


def test_manual_pair_jumps_over_higher_unpaired(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        _seed_pairings_table(conn)
        conn.execute(
            "INSERT INTO pairings"
            "(from_stable_id, to_stable_id, direction, source, "
            " created_at, modified_at) "
            "VALUES (?, ?, 'into', 'manual', '2026-01-01', '2026-01-01')",
            ("cur", "b"),
        )
        # "a" scores higher pre-bump; "b" gets the manual bump (+0.15)
        stage1 = [_sc("a", 0.60), _sc("b", 0.50)]
        out = rerank_with_pairings(
            conn=conn, current_stable_id="cur", stage1=stage1
        )
        assert [s.stable_id for s in out] == ["b", "a"]
        assert out[0].rationale.get("pair_manual") == 1.0
    finally:
        conn.close()


def test_learned_smaller_bump(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        _seed_pairings_table(conn)
        conn.execute(
            "INSERT INTO pairings"
            "(from_stable_id, to_stable_id, direction, source, "
            " created_at, modified_at) "
            "VALUES (?, ?, 'either', 'learned', '2026-01-01', '2026-01-01')",
            ("cur", "b"),
        )
        stage1 = [_sc("a", 0.60), _sc("b", 0.50)]
        out = rerank_with_pairings(
            conn=conn, current_stable_id="cur", stage1=stage1
        )
        # "b" gets +0.05 but "a" still wins (0.60 > 0.55).
        assert [s.stable_id for s in out] == ["a", "b"]
    finally:
        conn.close()


def test_pairings_direction_out_of_ignored(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        _seed_pairings_table(conn)
        conn.execute(
            "INSERT INTO pairings"
            "(from_stable_id, to_stable_id, direction, source, "
            " created_at, modified_at) "
            "VALUES (?, ?, 'out_of', 'manual', '2026-01-01', '2026-01-01')",
            ("cur", "b"),
        )
        stage1 = [_sc("a", 0.60), _sc("b", 0.50)]
        out = rerank_with_pairings(
            conn=conn, current_stable_id="cur", stage1=stage1
        )
        # "out_of" direction means "paired when b is the current"; for
        # current=cur we ignore it.
        assert [s.stable_id for s in out] == ["a", "b"]
    finally:
        conn.close()


@pytest.mark.parametrize("direction", ["out_of", "either"])
def test_reverse_stored_edge_bumps_its_partner(tmp_path, direction: str) -> None:
    """If (b, cur) is stored as out_of or either then b is bumped for cur, else stop."""
    conn = sqlite3.connect(str(tmp_path / "s.db"), isolation_level=None)
    try:
        _seed_pairings_table(conn)
        conn.execute(
            "INSERT INTO pairings"
            "(from_stable_id, to_stable_id, direction, source, "
            " created_at, modified_at) "
            "VALUES (?, ?, ?, 'manual', '2026-01-01', '2026-01-01')",
            ("b", "cur", direction),
        )
        stage1 = [_sc("a", 0.60), _sc("b", 0.50)]
        out = rerank_with_pairings(
            conn=conn, current_stable_id="cur", stage1=stage1
        )
        assert [s.stable_id for s in out] == ["b", "a"]
        assert out[0].rationale.get("pair_manual") == 1.0
    finally:
        conn.close()


def test_reverse_into_edge_is_not_a_pairing_for_its_target(tmp_path) -> None:
    """If (b, cur) is stored as into then cur's ranking is unchanged, else stop."""
    conn = sqlite3.connect(str(tmp_path / "s.db"), isolation_level=None)
    try:
        _seed_pairings_table(conn)
        conn.execute(
            "INSERT INTO pairings"
            "(from_stable_id, to_stable_id, direction, source, "
            " created_at, modified_at) "
            "VALUES ('b', 'cur', 'into', 'manual', '2026-01-01', '2026-01-01')"
        )
        stage1 = [_sc("a", 0.60), _sc("b", 0.50)]
        out = rerank_with_pairings(
            conn=conn, current_stable_id="cur", stage1=stage1
        )
        assert [s.stable_id for s in out] == ["a", "b"]
    finally:
        conn.close()
