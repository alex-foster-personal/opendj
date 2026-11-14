"""#12476: Tail Intrusion Ratio, outgoing-deck energy over the incoming deck's in the first bar after the handover."""

from __future__ import annotations

import json

import numpy as np

from apps.transition_score import scorer
from apps.transition_score.scorer import score_render, tail_intrusion_db
from tests.transition_score.synth import render

SR = 44100
SPAN = (0.0, 2.0)


def _noise(gain: float, seed: int) -> np.ndarray:
    return gain * np.random.default_rng(seed).standard_normal((2 * SR, 2))


def test_outgoing_18_db_below_reads_minus_18() -> None:
    """[if] outgoing-derived audio is 18 dB below the incoming deck over the first bar [then] TIR reads -18 dB, [else stop]."""
    tir = tail_intrusion_db(_noise(10 ** (-18 / 20), 1), _noise(1.0, 1), SR, SPAN)
    assert tir == -18.0


def test_outgoing_louder_reads_positive() -> None:
    """[if] outgoing-derived audio is louder than the incoming deck after handover [then] TIR is above 0 dB, [else stop]."""
    tir = tail_intrusion_db(_noise(2.0, 2), _noise(1.0, 3), SR, SPAN)
    assert tir is not None and tir > 0.0


def test_absent_outgoing_audio_is_unknown_not_fabricated() -> None:
    """[if] isolated outgoing audio is absent [then] TIR is unknown with a reason and no metric, [else stop]."""
    _, _, in_deck, ctx = render()
    t = scorer._Tally()
    scorer._tail_intrusion(t, None, in_deck, ctx)
    assert "tail_intrusion_db" not in t.metrics
    assert "mixed render" in t.unknown["tail_intrusion_db"]


def test_scored_candidate_serializes_tir_deterministically() -> None:
    """[if] a candidate is serialized after scoring [then] metrics_json carries the same TIR on every run, [else stop]."""
    first = json.loads(score_render(*render()).metrics_json)["tail_intrusion_db"]
    second = json.loads(score_render(*render()).metrics_json)["tail_intrusion_db"]
    assert isinstance(first, float) and first == second
