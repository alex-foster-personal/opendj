"""#10144: a window-scoped grid trust tier (BAR, BEAT, NONE) bounds the legal transition lengths."""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.autodj import propose as propose_module
from apps.autodj.contract.models import GRID_BAR, GRID_BEAT, GRID_NONE
from apps.autodj.plan import parse_plan, validate_plan
from apps.autodj.profile import profile_from_payloads
from apps.autodj.propose import propose_transition
from apps.autodj.styles import STYLES, list_styles
from apps.autodj.windows import mix_windows
from tests.autodj.fixtures import anlz_payload, profile, track_row

_REQ = "TRANSAI-14269"
OUT, IN = profile("trk-out"), profile("trk-in", key="9A")


def _profile_with(stable_id: str, **beatgrid: object):
    anlz = anlz_payload(stable_id)
    anlz["beatgrid"].update(beatgrid)
    return profile_from_payloads(anlz, track_row(stable_id))


def _served_with_tempo_changes(stable_id: str, at_s: list[float]):
    """The /anlz shape: ``tempo_changes`` is a top-level field of lane rows (own_beatgrid_overlay.py)."""
    anlz = anlz_payload(stable_id)
    anlz["tempo_changes"] = [{"at_s": t, "bpm_before": 128.0, "bpm_after": 130.0, "confidence": 0.9} for t in at_s]
    return profile_from_payloads(anlz, track_row(stable_id))


@pytest.mark.requirement("TRANSAI-14835")
def test_a_served_tempo_change_makes_its_span_beat_tier() -> None:
    """[if] /anlz serves tempo_changes[].at_s inside a span [then] that span is BEAT tier, [else stop]."""
    changed = _served_with_tempo_changes("trk-served", [127 * OUT.bar_s])
    assert changed.grid_tier(120, 16) == replace(changed.grid_tier(120, 16), tier=GRID_BEAT)
    assert changed.grid_tier(1, 16).tier == GRID_BAR
    # Control: the key nobody serves (beatgrid.tempo_markers) is not read.
    invented = _profile_with("trk-invented", tempo_markers=[{"t": 127 * OUT.bar_s}])
    assert invented.grid_tier(120, 16).tier == GRID_BAR


@pytest.mark.requirement(_REQ)
def test_grid_tier_bar_beat_none_from_the_served_grid() -> None:
    """[if] a grid is steady, has a change point, or fails GRIDFLAG [then] tier is BAR, BEAT or NONE, [else stop]."""
    assert OUT.grid_tier(1, 16).tier == GRID_BAR and OUT.grid_trusted
    bar_s = OUT.bar_s
    changed = _served_with_tempo_changes("trk-cp", [127 * bar_s])
    assert changed.grid_tier(120, 16).tier == GRID_BEAT
    assert changed.grid_tier(120, 16).reason == "change point at bar 128"
    assert changed.grid_tier(1, 16).tier == GRID_BAR and changed.grid_trusted
    dynamic = _profile_with("trk-dyn", static_grid_untrusted=True, reason="dynamic tempo")
    assert dynamic.grid_tier(1, 8) == replace(dynamic.grid_tier(1, 8), tier=GRID_BEAT)
    assert not dynamic.grid_trusted
    failed = _profile_with("trk-flag", gridflag_verdict="fail")
    assert failed.grid_tier(1, 8).tier == GRID_NONE and not failed.grid_trusted


@pytest.mark.requirement(_REQ)
def test_style_declares_legal_bars_by_tier_and_lists_it() -> None:
    """[if] styles are listed [then] each reports legal bars per tier: BAR max, BEAT <= 2, NONE 0 when beatmatched, [else stop]."""
    for doc in list_styles():
        style = STYLES[doc["name"]]
        legal = doc["legal_bars_by_tier"]
        assert legal[GRID_BAR] == style.max_bars
        assert legal[GRID_BEAT] <= 2
        assert (legal[GRID_NONE] == 0) == style.needs_beatmatch


@pytest.mark.requirement(_REQ)
def test_window_carries_tier_and_propose_rejects_without_clamping() -> None:
    """[if] the outgoing span is BEAT tier [then] windows carry it and long blends are rejected with the reason, [else stop]."""
    out = _profile_with("trk-out-dyn", static_grid_untrusted=True, reason="dynamic tempo")
    windows = mix_windows(out, "out", limit=6)
    assert windows and all(window.grid_tier == GRID_BEAT for window in windows)
    assert windows[0].summary()["grid_reason"] == "dynamic tempo"
    result = propose_transition(out, IN, out_deck=1, in_deck=2, intent="smooth", top=None)
    grid_rows = [row for row in result["rejected"] if row["gate"] == "grid"]
    assert grid_rows and all("BEAT tier at bars" in row["reason"] for row in grid_rows)
    for proposal in result["proposals"]:
        style = STYLES[proposal["style"]]
        assert parse_plan(proposal["plan"]).bars <= style.legal_bars_by_tier[GRID_BEAT]


@pytest.mark.requirement(_REQ)
def test_validate_plan_warns_when_the_length_exceeds_the_tier() -> None:
    """[if] a valid plan runs longer than its BEAT-tier legal length [then] validate_plan warns, not errors, [else stop]."""
    best = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth", top=1)["proposals"][0]
    plan = parse_plan(best["plan"])
    assert plan.bars > 2
    dynamic = _profile_with("trk-out", static_grid_untrusted=True, reason="dynamic tempo")
    clean = validate_plan(plan, OUT, IN)
    result = validate_plan(plan, dynamic, IN)
    assert result.errors == clean.errors
    assert any("BEAT tier at bars" in warning for warning in result.warnings)


@pytest.mark.requirement(_REQ)
def test_steady_grid_behaves_exactly_as_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] both grids are fully steady [then] propose output equals the run with the tier gate off, [else stop]."""
    with_tier = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth", top=None)
    monkeypatch.setattr(propose_module, "grid_tier_refusal", lambda *args: None)
    without = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth", top=None)
    assert with_tier == without
    assert all("grid_tier" not in window.summary() for window in mix_windows(OUT, "out", limit=6))
