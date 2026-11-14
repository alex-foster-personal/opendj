"""#12478: mid_clash_s, seconds both decks carry 300 Hz-3 kHz content near their solo level, reported and never scored."""

from __future__ import annotations

import json

import numpy as np
import pytest

from apps.transition_score import scorer
from apps.transition_score.scorer import MID_CLASH_ACTIVE_DB, mid_clash_seconds, score_render
from tests.transition_score.synth import render

HOP_S = 0.01


def test_both_decks_active_for_two_seconds_reads_two_seconds() -> None:
    """[if] both decks carry mid-band energy through two overlap seconds [then] mid_clash_s reads 2.0, [else stop]."""
    both = np.zeros(200)
    assert mid_clash_seconds(both, both, HOP_S) == 2.0


def test_one_deck_attenuated_below_the_line_reads_zero() -> None:
    """[if] either deck is attenuated below the active threshold [then] mid_clash_s reads 0, [else stop]."""
    full, cut = np.zeros(200), np.full(200, MID_CLASH_ACTIVE_DB - 0.5)
    assert mid_clash_seconds(full, cut, HOP_S) == 0.0
    assert mid_clash_seconds(cut, full, HOP_S) == 0.0


def test_empty_overlap_reads_zero() -> None:
    """[if] the overlap is empty [then] mid_clash_s reads 0 without error, [else stop]."""
    assert mid_clash_seconds(np.zeros(0), np.zeros(0), HOP_S) == 0.0


def test_mid_overlap_difference_moves_the_metric_not_m(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] candidates differ only in mid-band overlap [then] mid_clash_s differs and M, factors, vetoes do not, [else stop]."""
    assert mid_clash_seconds(np.zeros(300), np.zeros(300), HOP_S) != mid_clash_seconds(
        np.zeros(300), np.r_[np.zeros(100), np.full(200, -20.0)], HOP_S
    )
    measured = score_render(*render())
    assert json.loads(measured.metrics_json)["mid_clash_s"] >= 0.0
    monkeypatch.setattr(scorer, "_mid_clash", lambda *_: None)
    without = score_render(*render())
    assert (measured.score, measured.factors_json, measured.vetoes) == (
        without.score,
        without.factors_json,
        without.vetoes,
    )
