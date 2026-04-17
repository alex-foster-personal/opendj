"""Tests for :mod:`apps.sets.classify.rules`."""
from __future__ import annotations

import pytest

from apps.sets.classify import rules
from apps.sets.transitions import Transition


def _t(**features) -> Transition:
    t = Transition(
        idx=0, t_start_s=0.0, t_change_s=10.0, t_end_s=10.0,
        from_deck="A", to_deck="B", from_track=None, to_track=None,
    )
    t.features = features
    return t


@pytest.mark.requirement("SET-02")
def test_rules_cut_on_tiny_overlap():
    cls, conf = rules.classify(_t(overlap_s=0.2, fade_s=0.1, is_same_deck_reload=0))
    assert cls == "cut"
    assert conf == pytest.approx(0.9)


@pytest.mark.requirement("SET-02")
def test_rules_blend_on_medium_overlap():
    cls, conf = rules.classify(_t(overlap_s=12.0, fade_s=8.0, is_same_deck_reload=0))
    assert cls == "blend"
    assert conf == pytest.approx(0.75)


@pytest.mark.requirement("SET-02")
def test_rules_blend_on_long_overlap():
    cls, conf = rules.classify(_t(overlap_s=60.0, fade_s=40.0, is_same_deck_reload=0))
    assert cls == "blend"
    assert conf == pytest.approx(0.6)


@pytest.mark.requirement("SET-02")
def test_rules_quick_double_on_same_deck_reload():
    cls, conf = rules.classify(_t(overlap_s=2.0, fade_s=0.4, is_same_deck_reload=1.0))
    assert cls == "quick_double"
    assert conf == pytest.approx(0.85)


@pytest.mark.requirement("SET-02")
def test_rules_never_emits_filter_sweep():
    """Filter / FX require knob telemetry; rules must never fabricate them."""
    for overlap in (0.0, 1.0, 5.0, 20.0, 50.0, 120.0):
        cls, _ = rules.classify(_t(overlap_s=overlap, fade_s=1.0, is_same_deck_reload=0))
        assert cls in {"cut", "blend", "unknown"}


@pytest.mark.requirement("SET-02")
def test_rules_unknown_when_moderate_but_not_blend():
    cls, conf = rules.classify(_t(overlap_s=2.5, fade_s=1.2, is_same_deck_reload=0))
    assert cls == "unknown"
    assert conf == 0.0


@pytest.mark.requirement("SET-02")
def test_rules_classify_all_batches():
    res = rules.classify_all([
        _t(overlap_s=0.1, fade_s=0, is_same_deck_reload=0),
        _t(overlap_s=10.0, fade_s=5, is_same_deck_reload=0),
    ])
    assert [r[0] for r in res] == ["cut", "blend"]
