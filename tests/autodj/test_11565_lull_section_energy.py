"""#11565: lull_then_drop's lull test reads the outgoing start's own section energy, not the window mean."""

from __future__ import annotations

import pytest

from apps.autodj.propose import LULL_ENERGY, _fit_lull, lull_bars, place_hit
from apps.autodj.sections import INSTRUMENTAL
from apps.autodj.styles import STYLES
from apps.autodj.windows import MixWindow
from tests.autodj.fixtures import CHORUS, DOWN, INTRO, OUTRO, UP, profile

LATE_DROP = (
    (INTRO, 16, 0.4, 0.2, False),
    (UP, 16, 0.6, 0.5, False),
    (CHORUS, 32, 1.0, 1.0, False),
    (OUTRO, 16, 0.4, 0.3, False),
)
LULL = STYLES["lull_then_drop"]


def _out(arrangement: tuple, label: str, energy: float) -> tuple:
    out = profile("trk-out", arrangement=arrangement)
    return out, MixWindow("out", 49, 16, label, energy, None, None, 0.5, "test")


def _quiet_then_loud(label: str = INSTRUMENTAL, energy: float = 0.15) -> tuple:
    return (
        (INTRO, 16, 1.0, 1.0, False),
        (CHORUS, 32, 1.0, 1.0, False),
        (label, 16, energy, 0.1, False),
        (OUTRO, 16, 1.0, 1.0, False),
    )


@pytest.mark.requirement("TRANSAI-14523")
def test_quiet_instrumental_before_loud_outro_is_a_lull_from_its_section_energy() -> None:
    """[if] the start sits in a 16-bar instrumental at section energy 0.15 with window energy 0.71 [then] it is a lull, [else stop]."""
    out, window = _out(_quiet_then_loud(), INSTRUMENTAL, 0.71)
    section = out.section_at(49)
    assert section is not None and section.energy is not None and section.energy <= LULL_ENERGY
    assert lull_bars(out, 49) == 16
    placed = place_hit(LULL, window, profile("trk-in", arrangement=LATE_DROP), 1, 16, out_profile=out)
    assert placed.refused is None or "not a lull" not in placed.refused


@pytest.mark.requirement("TRANSAI-14523")
def test_place_hit_and_fit_lull_agree_on_the_quiet_instrumental() -> None:
    """[if] _fit_lull sees 16 lull bars [then] place_hit does not refuse lull_then_drop as not a lull, [else stop]."""
    out, window = _out(_quiet_then_loud(), INSTRUMENTAL, 0.71)
    assert _fit_lull(LULL, out, 49, 16, 1.0) == 16
    placed = place_hit(LULL, window, profile("trk-in", arrangement=LATE_DROP), 1, 16, out_profile=out)
    assert placed.refused is None or "not a lull" not in placed.refused


@pytest.mark.requirement("TRANSAI-14523")
def test_a_loud_breakdown_is_still_a_lull_by_its_label() -> None:
    """[if] the start sits in a breakdown, even a loud one with a loud window [then] it is still a lull, [else stop]."""
    out, window = _out(_quiet_then_loud(DOWN, 0.9), DOWN, 0.9)
    assert lull_bars(out, 49) > 0
    placed = place_hit(LULL, window, profile("trk-in", arrangement=LATE_DROP), 1, 16, out_profile=out)
    assert placed.refused is None or "not a lull" not in placed.refused


@pytest.mark.requirement("TRANSAI-14523")
def test_a_loud_section_is_refused_even_when_the_window_mean_is_quiet() -> None:
    """[if] the start section is loud and not breakdown/bridge [then] lull_then_drop is refused as not a lull, [else stop]."""
    arrangement = (
        (INTRO, 16, 0.1, 0.1, False),
        (CHORUS, 32, 0.1, 0.1, False),
        (INSTRUMENTAL, 16, 1.0, 1.0, False),
        (OUTRO, 16, 0.1, 0.1, False),
    )
    out, window = _out(arrangement, INSTRUMENTAL, 0.3)
    assert lull_bars(out, 49) == 0
    placed = place_hit(LULL, window, profile("trk-in", arrangement=LATE_DROP), 1, 16, out_profile=out)
    assert placed.refused is not None and "not a lull" in placed.refused
