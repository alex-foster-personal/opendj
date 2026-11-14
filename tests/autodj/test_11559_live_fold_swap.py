"""#11559: live proposals place the bass swap on the fold live Beat Sync uses, not the profile-only fold."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from apps.autodj import propose as propose_mod
from apps.autodj.plan import live_beat_sync_fold, pair_beat_sync_fold
from apps.autodj.profile import TrackProfile
from apps.autodj.propose import propose_transition
from tests.autodj.fixtures import profile

OUT = profile("trk-out", bpm=64.0)
_IN = profile("trk-in", bpm=128.0)
#: Incoming bass lands at its bar 12; the profile carries the 6% default range so only the mirror says WIDE.
IN: TrackProfile = replace(_IN, bar_bass=[0.1] * 11 + [1.0] * (_IN.bars - 11), deck_pitch_range_pct=6.0)


def _wide_live() -> dict[str, Any]:
    return {
        "master_deck": 1,
        "decks": {
            "1": {"stable_id": "trk-out", "playing": True, "effective_bpm": 64.0, "pitch_range": 8.0},
            "2": {"stable_id": "trk-in", "playing": False, "effective_bpm": 128.0, "pitch_range": 100.0},
        },
    }


def _folds_seen(monkeypatch: pytest.MonkeyPatch, live: dict[str, Any] | None) -> list[tuple[float, Any]]:
    seen: list[tuple[float, Any]] = []
    real = propose_mod.place_hit

    def spy(style, out_window, in_profile, in_start, bars, fold=1.0, out_profile=None):  # noqa: ANN001, ANN202
        hit = real(style, out_window, in_profile, in_start, bars, fold, out_profile)
        if style.tempo_mode == "sync_to_out":
            seen.append((fold, hit))
        return hit

    monkeypatch.setattr(propose_mod, "place_hit", spy)
    propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth", live=live, top=None)
    return seen


@pytest.mark.requirement("AUTODJ-14612")
def test_live_wide_range_uses_fold_one_not_profile_fold_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] live 64 out, 128 in, incoming WIDE [then] placement uses live fold 1, not fold 2 from profiles, [else stop]."""
    assert pair_beat_sync_fold(OUT.judged_bpm, IN.judged_bpm, 6.0) == 2.0
    assert live_beat_sync_fold(_wide_live(), 1, 2, IN) == 1.0
    folds = {fold for fold, _ in _folds_seen(monkeypatch, _wide_live())}
    assert folds == {1.0}


@pytest.mark.requirement("AUTODJ-14612")
def test_live_fold_one_swap_waits_for_the_incoming_bass(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a blend's incoming bass lands under live fold 1 [then] the swap is not placed before that bass, [else stop]."""
    placed = [hit for _, hit in _folds_seen(monkeypatch, _wide_live()) if hit.refused is None and hit.entry]
    assert placed
    for hit in placed:
        real_entry = hit.entry["in_bar"] - hit.in_start_bar
        assert hit.entry["entry_plan_bar"] == real_entry
        assert hit.hit_bar >= real_entry


@pytest.mark.requirement("AUTODJ-14612")
def test_offline_keeps_the_profile_only_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] there is no live mirror [then] placement still uses the profile-only fold, [else stop]."""
    assert live_beat_sync_fold(None, 1, 2, IN) is None
    folds = {fold for fold, _ in _folds_seen(monkeypatch, None)}
    assert folds == {2.0}


@pytest.mark.requirement("AUTODJ-14612")
def test_a_partial_mirror_falls_back_to_the_profile_fold() -> None:
    """[if] the mirror lacks the incoming pitch range [then] the live fold is None and the profile fold stays, [else stop]."""
    live = _wide_live()
    del live["decks"]["2"]["pitch_range"]
    assert live_beat_sync_fold(live, 1, 2, IN) is None
