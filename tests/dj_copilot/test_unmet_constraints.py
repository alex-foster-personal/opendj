"""Shape of the UnmetConstraint surface (PLAY-02)."""
from __future__ import annotations

import pytest

from apps.dj_copilot.solver import UnmetConstraint

pytestmark = pytest.mark.requirement("PLAY-02")


def test_fields() -> None:
    uc = UnmetConstraint(
        kind="bpm_window", position=3, detail={"delta_pct": 11.5, "stable_id": "x"}
    )
    assert uc.kind == "bpm_window"
    assert uc.position == 3
    assert uc.detail["delta_pct"] == 11.5


def test_accepts_known_kinds() -> None:
    for kind in ("bpm_window", "camelot_hardcut", "artist_repeat", "energy_miss"):
        UnmetConstraint(kind=kind, position=0, detail={})
