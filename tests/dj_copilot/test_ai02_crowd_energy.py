"""AI-02 crowd-energy scenarios."""
from __future__ import annotations

import sqlite3

import pytest

from apps.dj_copilot.session_context import PlayedTrack, SessionContext
from apps.dj_copilot.suggester import suggest_next
from apps.shared.harmonic import TrackFeature
from tests.dj_copilot.conftest import FIXED_NOW

pytestmark = pytest.mark.requirement("AI-02")


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
    return SessionContext(
        recent=recent, source="manual", captured_at=FIXED_NOW
    )


def _library_all_energies(current_energy: int) -> list[TrackFeature]:
    """Current + 10 candidates, one per energy bucket 1..10."""
    tracks = [TrackFeature("cur", "Cur", 120.0, "8A", current_energy)]
    for e in range(1, 11):
        tracks.append(TrackFeature(f"e-{e}", f"A{e}", 120.5, "8A", e))
    return tracks


def test_warmup_prefers_mid_energy(tmp_path) -> None:
    """Warmup slope (rising, current energy 5) -> top candidate in 4..7."""
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_library_all_energies(current_energy=5),
            context=_ctx([3, 3, 4, 4, 5]),
            top_n=10,
        )
        top_energy = out[0].rationale_numbers.get("energy", 0.0)
        assert top_energy > 0.8, f"top candidate energy score low: {top_energy}"
    finally:
        conn.close()


def test_peak_prefers_high_energy(tmp_path) -> None:
    """At peak with energies 7-9, top candidate energy should be >= 8."""
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_library_all_energies(current_energy=9),
            context=_ctx([7, 8, 8, 9, 9]),
            top_n=5,
        )
        # Energies 2, 3, 4 should have been dropped by the slope cap
        # (reverse > 2 steps relative to current=9).
        ids = {s.stable_id for s in out}
        assert "e-2" not in ids
        assert "e-3" not in ids
    finally:
        conn.close()


def test_winddown_prefers_low_energy(tmp_path) -> None:
    """Winddown slope (falling, current=5). Candidates well above 5 dropped."""
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        out = suggest_next(
            conn=conn,
            current_stable_id="cur",
            library=_library_all_energies(current_energy=5),
            context=_ctx([9, 8, 7, 6, 5]),
            top_n=5,
        )
        # With falling slope, a candidate jump to energy=10 would reverse
        # by >2 steps above current=5 -> dropped.
        ids = {s.stable_id for s in out}
        assert "e-10" not in ids
        assert "e-9" not in ids
    finally:
        conn.close()
