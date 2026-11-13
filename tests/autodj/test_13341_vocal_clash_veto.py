"""Long blends are refused on a measured vocal clash (#13341, spec d27ed712) on invented tracks."""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.autodj import propose
from apps.autodj.profile import TrackProfile
from apps.autodj.propose import propose_transition
from apps.autodj.scoring import score_windows
from apps.autodj.styles import STYLES
from apps.autodj.windows import window_at
from tests.autodj.fixtures import profile

REQ = "AGENT-13818"


@pytest.fixture(autouse=True)
def _no_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """The veto itself is under test here; #10152's repair ladder is tested in test_10152_repair_loop.py."""
    monkeypatch.setattr(propose, "REPAIR_ROUNDS", 0)


OUT_BAR, IN_BAR, CLASH_BARS = 97, 10, (100, 101)


def _pair(
    out_level: float, *, out_map: bool = True, in_map: bool = True, clash_bars: tuple[int, ...] = CLASH_BARS
) -> tuple[TrackProfile, TrackProfile]:
    """Outgoing vocals only on ``clash_bars`` inside the 97+ overlap at ``out_level``; incoming vocals on every bar."""
    out, incoming = profile("trk-out"), profile("trk-in", key="9A")
    out_vocals = tuple(out_level if bar in clash_bars else 0.0 for bar in range(1, out.bars + 1))
    in_vocals = tuple(0.25 for _ in range(incoming.bars))
    return (
        replace(out, bar_vocals=out_vocals if out_map else None),
        replace(incoming, bar_vocals=in_vocals if in_map else None),
    )


def _propose(out: TrackProfile, incoming: TrackProfile, intent: str) -> dict:
    return propose_transition(out, incoming, out_deck=1, in_deck=2, intent=intent, top=None, around_bar=113)


def _clash(out: TrackProfile, incoming: TrackProfile) -> int | None:
    """The real scored clash count on the one blend window this setup offers (out 97, in 10, 31 bars)."""
    pair = score_windows(
        out, incoming, window_at(out, "out", OUT_BAR), window_at(incoming, "in", IN_BAR), overlap_bars=31
    )
    return pair.vocal_clash_bars


def _blends(result: dict) -> list[dict]:
    return [row for row in result["proposals"] if row["style"] == "bass_swap_blend"]


@pytest.mark.requirement(REQ)
def test_a_measured_two_bar_clash_refuses_the_long_blend() -> None:
    """[if] a 16-bar-default blend measures 2 clashing vocal bars [then] it is refused in not_offered, [else stop]."""
    out, incoming = _pair(0.25)
    assert _clash(out, incoming) == 2
    result = _propose(out, incoming, "smooth")
    assert not _blends(result)
    rows = [
        row for row in result["not_offered"] if row["style"] == "bass_swap_blend" and "vocal clash" in row["reason"]
    ]
    assert rows and rows[0]["out_bar"] == OUT_BAR and rows[0]["in_bar"] == IN_BAR
    assert "2 measured" in rows[0]["reason"] and "limit 0" in rows[0]["reason"]


@pytest.mark.requirement(REQ)
@pytest.mark.parametrize("intent", ["energy_up", "showy"])
def test_cut_and_drop_swap_stay_eligible_on_the_clashing_pair(intent: str) -> None:
    """[if] the pair clashes [then] a valid cut or drop_swap is still returned, [else stop]."""
    out, incoming = _pair(0.25)
    shorts = [row for row in _propose(out, incoming, intent)["proposals"] if row["style"] in {"cut", "drop_swap"}]
    assert shorts and all(row["validation"]["ok"] for row in shorts)


@pytest.mark.requirement(REQ)
def test_smooth_falls_back_to_a_cut_with_a_truthful_rationale() -> None:
    """[if] every smooth blend is refused for vocal clash [then] the cut fallback says no blend was eligible, [else stop]."""
    out, incoming = _pair(0.25)
    cuts = [row for row in _propose(out, incoming, "smooth")["proposals"] if row["style"] == "cut"]
    assert cuts and all(row["validation"]["ok"] for row in cuts)
    assert all("no preferred blend was eligible" in row["rationale"] for row in cuts)


@pytest.mark.requirement(REQ)
def test_zero_measured_overlap_keeps_the_blend() -> None:
    """[if] vocals sit just under the 0.25 threshold [then] clash is 0 and the blend returns, [else stop]."""
    out, incoming = _pair(0.24)
    assert _clash(out, incoming) == 0
    assert _blends(_propose(out, incoming, "smooth"))


@pytest.mark.requirement(REQ)
@pytest.mark.parametrize(("out_map", "in_map"), [(False, True), (True, False), (False, False)])
def test_a_missing_vocal_map_is_unscored_and_never_refuses(out_map: bool, in_map: bool) -> None:
    """[if] either vocal map is missing [then] clash is None, vocals unscored, the blend returns, [else stop]."""
    out, incoming = _pair(0.25, out_map=out_map, in_map=in_map)
    pair = score_windows(
        out, incoming, window_at(out, "out", OUT_BAR), window_at(incoming, "in", IN_BAR), overlap_bars=31
    )
    assert pair.vocal_clash_bars is None and "vocals" in dict(pair.unscored)
    result = _propose(out, incoming, "smooth")
    assert _blends(result)
    assert not any("vocal clash" in row["reason"] for row in result["not_offered"])


@pytest.mark.requirement(REQ)
def test_an_eight_bar_default_style_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a blend defaults to exactly 8 bars and clashes [then] it is refused for vocal clash, [else stop]."""
    monkeypatch.setitem(STYLES, "bass_swap_blend", replace(STYLES["bass_swap_blend"], default_bars=8))
    out, incoming = _pair(0.25)
    result = _propose(out, incoming, "smooth")
    assert not _blends(result)
    assert any(row["style"] == "bass_swap_blend" and "8-bar-default" in row["reason"] for row in result["not_offered"])


def _vocal_refusals(result: dict) -> list[dict]:
    return [
        row for row in result["not_offered"] if row["style"] == "bass_swap_blend" and "vocal clash" in row["reason"]
    ]


@pytest.mark.requirement(REQ)
def test_an_eight_bar_default_style_is_refused_on_one_clash_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] default_bars is 8 and exactly one clash bar scores [then] the blend is refused naming the count, [else stop]."""
    monkeypatch.setitem(STYLES, "bass_swap_blend", replace(STYLES["bass_swap_blend"], default_bars=8))
    out, incoming = _pair(0.25, clash_bars=(100,))
    assert _clash(out, incoming) == 1
    result = _propose(out, incoming, "smooth")
    assert not _blends(result)
    rows = [row for row in _vocal_refusals(result) if row["out_bar"] == OUT_BAR and row["in_bar"] == IN_BAR]
    assert len(rows) == 1
    assert "1 measured" in rows[0]["reason"] and "8-bar-default" in rows[0]["reason"]


@pytest.mark.requirement(REQ)
def test_an_eight_bar_default_style_with_zero_clash_is_still_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] default_bars is 8 and the measured clash count is zero [then] the blend is offered, [else stop]."""
    monkeypatch.setitem(STYLES, "bass_swap_blend", replace(STYLES["bass_swap_blend"], default_bars=8))
    out, incoming = _pair(0.24)
    assert _clash(out, incoming) == 0
    assert _blends(_propose(out, incoming, "smooth"))


@pytest.mark.requirement(REQ)
@pytest.mark.parametrize(("default_bars", "expect_refused"), [(7, False), (8, True), (9, True)])
def test_the_vocal_gate_boundary_sits_at_eight_default_bars(
    monkeypatch: pytest.MonkeyPatch, default_bars: int, expect_refused: bool
) -> None:
    """[if] one clash bar is measured [then] refusal starts at default_bars 8, not 7 (#13342), [else stop]."""
    monkeypatch.setitem(STYLES, "bass_swap_blend", replace(STYLES["bass_swap_blend"], default_bars=default_bars))
    out, incoming = _pair(0.25, clash_bars=(100,))
    assert _clash(out, incoming) == 1
    result = _propose(out, incoming, "smooth")
    if expect_refused:
        assert not _blends(result)
        assert len(_vocal_refusals(result)) == 1
    else:
        assert _blends(result)
        assert not _vocal_refusals(result)
