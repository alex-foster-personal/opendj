"""AI-05 soft pressure prior on suggest-next.

[if] a release-pressure session ranks suggest-next [then] low-energy tracks get a small bump that never outranks a manual pairing, [else stop].
"""

from __future__ import annotations

import sqlite3

import pytest

from apps.dj_copilot.peak_pressure import KEEP_BUMP, RELEASE_BUMP
from apps.dj_copilot.rank_stage2 import _BUMPS
from apps.dj_copilot.session_context import PlayedTrack, SessionContext
from apps.dj_copilot.suggester import suggest_next
from apps.shared.harmonic import TrackFeature
from tests.dj_copilot.conftest import FIXED_NOW

pytestmark = pytest.mark.requirement("AI-05")


def _ctx(energies: list[int]) -> SessionContext:
    recent = [
        PlayedTrack(
            stable_id=f"r-{i}",
            artist=f"R{i}",
            bpm=120.0,
            key_camelot="8A",
            energy=e,
            played_at=FIXED_NOW,
        )
        for i, e in enumerate(energies)
    ]
    return SessionContext(recent=recent, source="manual", captured_at=FIXED_NOW)


def _library_all_energies(current_energy: int) -> list[TrackFeature]:
    tracks = [TrackFeature("cur", "Cur", 120.0, "8A", current_energy)]
    tracks.extend(TrackFeature(f"e-{e}", f"A{e}", 120.5, "8A", e) for e in range(1, 11))
    return tracks


def _library_with_unknown_energy(current_energy: int) -> list[TrackFeature]:
    tracks = _library_all_energies(current_energy)
    tracks.append(TrackFeature("unknown", "NoEnergy", 120.5, "8A", None))
    return tracks


def test_release_session_keeps_high_energy_candidates(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_library_all_energies(current_energy=9),
            context=_ctx([9, 9, 9, 9]),
            top_n=10,
        )
        ids = {s.stable_id for s in out}
        assert any(s.stable_id.startswith("e-") and s.candidate and s.candidate.energy >= 8 for s in out), (
            "high-energy candidates must remain listed"
        )
        assert "e-10" in ids or "e-9" in ids or "e-8" in ids
    finally:
        conn.close()


def test_release_session_bumps_low_energy_with_tag(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        with_session = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_library_all_energies(current_energy=9),
            context=_ctx([9, 9, 9, 9]),
            top_n=10,
        )
        without_session = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_library_all_energies(current_energy=9),
            context=None,
            top_n=10,
        )
        low_with = next(s for s in with_session if s.stable_id == "e-5")
        low_without = next(s for s in without_session if s.stable_id == "e-5")
        assert "release_a_little" in low_with.rationale_tags
        assert low_with.score > low_without.score
    finally:
        conn.close()


def test_manual_pairing_outranks_release_bump() -> None:
    assert _BUMPS["manual"] > RELEASE_BUMP


def test_keep_building_session_bumps_high_energy_candidates(tmp_path) -> None:
    """[if] recent low-energy plays imply keep_building [then] energy>=7 gains KEEP_BUMP [else stop]."""
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        library = _library_all_energies(current_energy=5)
        with_session = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=library,
            context=_ctx([5, 5, 5, 5]),
            top_n=10,
        )
        without_session = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=library,
            context=None,
            top_n=10,
        )
        high_with = next(s for s in with_session if s.stable_id == "e-8")
        high_without = next(s for s in without_session if s.stable_id == "e-8")
        assert high_with.score == pytest.approx(high_without.score + KEEP_BUMP)
        with_ids = {s.stable_id for s in with_session}
        assert "e-8" in with_ids and "e-9" in with_ids
    finally:
        conn.close()


@pytest.mark.parametrize(
    "session_energies",
    [[9, 9, 9, 9], [5, 5, 5, 5]],
    ids=["release", "keep_building"],
)
def test_pressure_prior_leaves_unknown_energy_candidate_unchanged(tmp_path, session_energies: list[int]) -> None:
    """[if] candidate energy is missing under pressure [then] score matches no-session [else stop]."""
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    current = session_energies[0]
    try:
        library = _library_with_unknown_energy(current_energy=current)
        with_session = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=library,
            context=_ctx(session_energies),
            top_n=12,
        )
        without_session = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=library,
            context=None,
            top_n=12,
        )
        unknown_with = next(s for s in with_session if s.stable_id == "unknown")
        unknown_without = next(s for s in without_session if s.stable_id == "unknown")
        assert unknown_with.score == unknown_without.score
    finally:
        conn.close()


def test_empty_session_ranking_unchanged(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        library = _library_all_energies(current_energy=5)
        baseline = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=library,
            context=None,
            top_n=10,
        )
        empty_ctx = SessionContext(recent=[], source="empty", captured_at=FIXED_NOW)
        with_empty = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=library,
            context=empty_ctx,
            top_n=10,
        )
        assert [s.stable_id for s in baseline] == [s.stable_id for s in with_empty]
    finally:
        conn.close()
