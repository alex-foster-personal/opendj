"""#10129: score_pair's energy term read from the MIK seam, its intent bands, and the cliff warning."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from apps.autodj.plan import parse_plan, validate_plan
from apps.autodj.scoring import TERM_WEIGHTS, energy_term, score_windows
from apps.autodj.windows import mix_windows
from tests.autodj.test_plan_propose_compile import IN, OUT, _plan


def _levels(marks: dict[int, int], base: int = 5, length: int = 400) -> tuple[int, ...]:
    return tuple(marks.get(bar, base) for bar in range(1, length + 1))


def _pair(out_marks: dict[int, int] | None, in_marks: dict[int, int] | None, intent: str | None = None):
    out_window = replace(mix_windows(OUT, "out", limit=1)[0], start_bar=101)
    in_window = replace(mix_windows(IN, "in", limit=1)[0], start_bar=1)
    out_p = OUT if out_marks is None else replace(OUT, mik_bar_level=_levels(out_marks))
    in_p = IN if in_marks is None else replace(IN, mik_bar_level=_levels(in_marks))
    return score_windows(out_p, in_p, out_window, in_window, overlap_bars=8, intent=intent)


@pytest.mark.requirement("TRANSAI-14326")
def test_seam_levels_read_last_overlap_bar_and_landing_bar() -> None:
    """[if] MIK series exist [then] out_level is at out.start+bars-1, in_level at in.start+bars, both in reasons, [else stop]."""
    pair = _pair({108: 6}, {9: 7})
    assert pair.energy is not None
    assert (pair.energy["out_level"], pair.energy["in_level"], pair.energy["seam_step"]) == (6, 7, 1)
    assert "energy 6 -> 7 (MIK track levels)" in pair.reasons


@pytest.mark.requirement("TRANSAI-14326")
def test_intent_bands_score_the_step() -> None:
    """[if] a seam step is scored [then] it is 1 inside the intent band and falls a third per level outside, [else stop]."""
    assert energy_term(0, "smooth") == 1.0 and energy_term(1, "vocals_clean") == 1.0
    assert energy_term(3, "smooth") == pytest.approx(1 / 3)
    assert energy_term(2, "energy_up") == 1.0 and energy_term(0, "energy_up") == pytest.approx(2 / 3)
    assert energy_term(-2, "wind_down") == 1.0 and energy_term(3, "wind_down") == 0.0
    assert energy_term(-6, "showy") == 1.0
    assert _pair({108: 5}, {9: 5}, intent="energy_up").energy["term"] == pytest.approx(2 / 3)


@pytest.mark.requirement("TRANSAI-14326")
def test_energy_weight_and_unscored_without_a_mik_series() -> None:
    """[if] a side has no MIK series [then] energy is unscored with its reason, weights sum to 1, energy 0.15, [else stop]."""
    assert TERM_WEIGHTS["energy"] == 0.15 and math.isclose(sum(TERM_WEIGHTS.values()), 1.0)
    pair = _pair({108: 6}, None)
    assert pair.energy is None
    assert ("energy", "a MIK energy series is missing") in pair.unscored
    assert all("MIK track levels" not in reason for reason in pair.reasons)


@pytest.mark.requirement("TRANSAI-14326")
def test_validate_plan_warns_an_energy_cliff_inside_a_blend_only() -> None:
    """[if] the seam steps 4+ levels [then] a blend gets a cliff warning, never an error, and a cut gets none, [else stop]."""
    out_p = replace(OUT, mik_bar_level=_levels({127: 7}))
    in_p = replace(IN, mik_bar_level=_levels({16: 3}))
    blend = validate_plan(parse_plan(_plan()), out_p, in_p)
    assert "energy cliff: level 7 into 3 inside a blend" in blend.warnings
    assert not any("energy cliff" in error for error in blend.errors)
    cut = parse_plan({**_plan(), "style": "cut"})
    assert not any("energy cliff" in w for w in validate_plan(cut, out_p, in_p).warnings)


@pytest.mark.requirement("TRANSAI-14326")
def test_summary_carries_energy_or_null() -> None:
    """[if] score_pair's JSON is built [then] it carries energy {out_level, in_level, seam_step, term} or null, [else stop]."""
    assert _pair({108: 6}, {9: 7}).summary()["energy"] == {"out_level": 6, "in_level": 7, "seam_step": 1, "term": 1.0}
    assert _pair(None, None).summary()["energy"] is None
