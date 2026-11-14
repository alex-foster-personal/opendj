"""MIK energy falls and rises in mix windows, and MIK boundaries as sections (#10126)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from apps.autodj.profile import profile_from_payloads
from apps.autodj.windows import mix_windows
from tests.autodj.fixtures import DOWN, INTRO, anlz_payload, profile, track_row

_REQ = "TRANSAI-14224"
_BAR_S = 4 * 60.0 / 128.0


def _segments(levels: list[tuple[int, int]], bars: int) -> list[dict[str, Any]]:
    """(start bar, level) rows as /anlz energy segments on the 128 bpm fixture grid."""
    rows = []
    for index, (bar, level) in enumerate(levels):
        end_bar = levels[index + 1][0] if index + 1 < len(levels) else bars + 1
        rows.append({"start_s": (bar - 1) * _BAR_S, "end_s": (end_bar - 1) * _BAR_S, "level": level, "source": "mik"})
    return rows


def _with_mik(anlz: dict[str, Any], levels: list[tuple[int, int]], bars: int) -> dict[str, Any]:
    anlz["energy_source"] = "mik"
    anlz["energy_level"] = levels[0][1]
    anlz["energy_segments"] = _segments(levels, bars)
    return anlz


def _fall_profile() -> Any:
    anlz = _with_mik(anlz_payload("mik-fall"), [(1, 7), (81, 5), (97, 4), (121, 2)], 128)
    return profile_from_payloads(anlz, track_row("mik-fall", bars=128))


@pytest.mark.requirement(_REQ)
def test_last_mik_fall_before_the_outro_ranks_first_among_falls() -> None:
    """[if] MIK falls sit in the back half before the outro [then] they gain a fall term and the last ranks first, [else stop]."""
    track = _fall_profile()
    plain = {w.start_bar: w.score for w in mix_windows(replace(track, mik_bar_level=None), "out", limit=50)}
    windows = mix_windows(track, "out", limit=50)
    falls = [w for w in windows if w.reason.startswith("energy falls")]
    assert [w.start_bar for w in falls] == [97, 81]
    assert falls[0].score > falls[1].score
    assert all(w.start_bar < 113 for w in falls)  # bar 121 is after the outro
    assert next(w for w in windows if w.start_bar == 81).score == pytest.approx(max(plain.get(81, 0.0), falls[1].score))


@pytest.mark.requirement(_REQ)
def test_in_window_lands_on_a_mik_rise() -> None:
    """[if] an in window ends on a MIK step up of 2 or more [then] landing is 1 and reason names the rise, [else stop]."""
    arrangement = ((INTRO, 16, 0.5, 0.2, False), (DOWN, 16, 0.4, 0.1, True))
    anlz = _with_mik(anlz_payload("mik-rise", arrangement=arrangement), [(1, 4), (17, 7)], 32)
    track = profile_from_payloads(anlz, track_row("mik-rise", bars=32))
    base = mix_windows(replace(track, mik_bar_level=None), "in")[0]
    risen = mix_windows(track, "in")[0]
    assert risen.start_bar == base.start_bar == 1
    assert risen.score == pytest.approx(base.score + 0.15)
    assert "lands on an energy rise 4 to 7 at bar 17" in risen.reason


@pytest.mark.requirement(_REQ)
def test_mik_boundaries_become_sections_only_when_no_phrases_are_served() -> None:
    """[if] phrases_source is none [then] MIK boundaries become energy sections, else PSSI sections stay, [else stop]."""
    levels = [(1, 5), (33, 8), (65, 6), (97, 6)]
    bare = _with_mik(anlz_payload("mik-bare"), levels, 128)
    bare["phrases"] = []
    bare["phrases_source"] = "none"
    track = profile_from_payloads(bare, track_row("mik-bare", bars=128))
    assert track.sections_source == "mik-energy"
    assert [(s.label, s.start_bar) for s in track.sections] == [
        ("energy_up", 33),
        ("energy_down", 65),
        ("energy_flat", 97),
    ]
    assert mix_windows(track, "out")
    served = profile_from_payloads(_with_mik(anlz_payload("mik-pssi"), levels, 128), track_row("mik-pssi", bars=128))
    assert served.sections_source != "mik-energy"
    assert all(not s.label.startswith("energy_") for s in served.sections)


@pytest.mark.requirement(_REQ)
def test_no_mik_scores_exactly_as_before() -> None:
    """[if] mik_bar_level is None [then] every window equals the no-MIK golden, [else stop]."""
    track = profile("trk-a")
    assert track.mik_bar_level is None
    for role in ("out", "in"):
        assert mix_windows(track, role, limit=50) == mix_windows(replace(track, mik_boundaries=()), role, limit=50)
    stale = replace(_fall_profile(), mik_bar_level=None)
    for role in ("out", "in"):
        assert mix_windows(stale, role, limit=50) == mix_windows(replace(stale, mik_boundaries=()), role, limit=50)


@pytest.mark.requirement(_REQ)
def test_reason_names_the_mik_fall() -> None:
    """[if] a window uses a MIK fall [then] its reason names the levels and bar, [else stop]."""
    reasons = [w.reason for w in mix_windows(_fall_profile(), "out", limit=50)]
    assert any(r.startswith("energy falls 7 to 5 at bar 81") for r in reasons)
