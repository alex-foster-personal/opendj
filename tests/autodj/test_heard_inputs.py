"""AUTOPLAY-HEARD: the pair is judged on the key and tempo the decks play, not the stored track values.

Worked case (Tue 3 Nov 2026 post-mortem): Monster stored 9A, playing shifted +1 so heard 4A; the pick When The Sun
Goes Down at 10A was a 6-step clash as heard. With the fix the pair is judged 4A vs 10A, and Sun loading +1 (5A) fits.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from apps.autodj.contract import KeySegment
from apps.autodj.live import heard_on_deck
from apps.autodj.plan import bpm_gap_pct
from apps.autodj.profile import heard_profile
from apps.autodj.scoring import key_at, score_windows
from apps.autodj.windows import mix_windows
from apps.shared.harmonic import shift_camelot
from tests.autodj.fixtures import profile

MONSTER = replace(
    profile("monster", key="9A", bpm=120.0),
    key_segments=(KeySegment(start_bar=1, end_bar=64, key="9A"),),
)
SUN = profile("sun", key="10A", bpm=120.0)


def _deck(stable_id: str, *, shift: int, effective_bpm: float) -> dict[str, Any]:
    return {"stable_id": stable_id, "effective_bpm": effective_bpm, "key_sync": {"shift_semitones": shift}}


def _score(out: Any, in_: Any) -> Any:
    return score_windows(out, in_, mix_windows(out, "out", limit=1)[0], mix_windows(in_, "in", limit=1)[0])


@pytest.mark.requirement("AUTOPLAY-8868")
def test_shift_camelot_matches_the_webui_wheel() -> None:
    """[if] a key is shifted by semitones [then] it moves 7 wheel steps per semitone, mode kept, [else stop]."""
    assert shift_camelot("9A", 1) == "4A", "if Em shifted up a semitone is not Fm (4A) then broken"
    assert shift_camelot("10A", 1) == "5A"
    assert shift_camelot("8B", -1) == "1B"
    assert shift_camelot("4A", 0) == "4A"


@pytest.mark.requirement("AUTOPLAY-8868")
def test_monster_to_sun_is_judged_at_the_heard_key() -> None:
    """[if] the master plays shifted +1 [then] the pair is judged on its heard key 4A, not stored 9A, [else stop]."""
    heard = heard_on_deck(MONSTER, _deck("monster", shift=1, effective_bpm=120.0), with_tempo=True)
    assert heard.key == "4A" and key_at(heard, 10) == "4A", "if a segment keeps the stored key then broken"
    stored = _score(MONSTER, SUN)
    as_heard = _score(heard, SUN)
    assert stored.key_distance == 1, "control: the stored keys 9A vs 10A are one step apart"
    assert as_heard.key_distance == 6, "if the heard 4A vs 10A clash is not seen then broken"
    shifted_in = heard_profile(SUN, key_shift=1)
    assert _score(heard, shifted_in).key_distance == 1, "if Sun loaded +1 (5A) does not fit 4A then broken"


@pytest.mark.requirement("AUTOPLAY-8868")
def test_a_deck_holding_another_track_or_no_shift_leaves_the_profile_alone() -> None:
    """[if] the deck holds another track or no shift [then] the stored profile stands, [else stop]."""
    assert heard_on_deck(MONSTER, _deck("other", shift=3, effective_bpm=130.0), with_tempo=True) is MONSTER
    unshifted = heard_on_deck(MONSTER, {"stable_id": "monster"}, with_tempo=True)
    assert unshifted is MONSTER, "if a deck with no key_sync or tempo changes the profile then broken"


@pytest.mark.requirement("AUTOPLAY-8868")
def test_heard_profile_records_integer_key_shift() -> None:
    """[if] key_shift is non-zero [then] heard_key_shift is set, [else stop]."""
    shifted = heard_profile(SUN, key_shift=1)
    assert shifted.heard_key_shift == 1
    assert heard_profile(SUN, key_shift=0) is SUN


@pytest.mark.requirement("AUTOPLAY-8868b")
def test_the_tempo_gap_is_measured_at_the_masters_heard_tempo() -> None:
    """[if] the master is pitched to 126 [then] the gap is from 126 and the bar clock stays on its own BPM, [else stop]."""
    heard = heard_on_deck(MONSTER, _deck("monster", shift=0, effective_bpm=126.0), with_tempo=True)
    assert heard.judged_bpm == 126.0 and heard.bpm == 120.0, "if the bar clock moved to the heard tempo then broken"
    assert heard.bar_at(30.0) == MONSTER.bar_at(30.0)
    assert _score(heard, profile("in126", bpm=126.0)).bpm_gap_pct == pytest.approx(0.0)
    assert _score(MONSTER, profile("in126", bpm=126.0)).bpm_gap_pct == pytest.approx(bpm_gap_pct(120.0, 126.0))
