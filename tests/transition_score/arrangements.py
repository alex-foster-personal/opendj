"""Shared fixture arrangements for the transition_score tests (section, bars, energy, bass, peak)."""

from __future__ import annotations

from tests.autodj.fixtures import CHORUS, INTRO, OUTRO, UP

ARRANGEMENT = ((INTRO, 16, 0.4, 0.3, False), (CHORUS, 16, 1.0, 1.0, True), (OUTRO, 16, 0.4, 0.8, False))
#: The incoming track carries a 16-bar bass build before its chorus, so a blend entered where its bass lands
#: still has the 16 bars (64 beats) of forward mixability blends need (AUTODJ-11851).
IN_ARRANGEMENT = (
    (INTRO, 16, 0.4, 0.3, False),
    (UP, 16, 0.6, 1.0, False),
    (CHORUS, 16, 1.0, 1.0, True),
    (OUTRO, 16, 0.4, 0.8, False),
)
IN_BARS = sum(row[1] for row in IN_ARRANGEMENT)
