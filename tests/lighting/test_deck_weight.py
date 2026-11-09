"""LIGHT-9861: pure deck mix weights from channel faders, assign and crossfader.

[if] blend at centre crossfader does not halve both sides [then] fail, [else stop].
[if] upfader_only with crossfader hard A does not keep deck B's fader as weight [then] fail, [else stop].
[if] master_only does not give exactly one deck weight 1.0 and others 0.0 [then] fail, [else stop].
[if] a THRU deck is scaled by the crossfader in blend [then] fail, [else stop].
[if] an unknown mode does not name the allowed modes [then] fail, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.lighting.deck_weight import DeckWeightMode, crossfader_gain, deck_weights

pytestmark = [pytest.mark.requirement("LIGHT-9861")]


def _ch(deck_id: int, fader: float, assign: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "deck_id": deck_id,
        "trim": 0.5,
        "eq_high": 0.5,
        "eq_mid": 0.5,
        "eq_low": 0.5,
        "filter": 0.5,
        "fader": fader,
        "assign": assign,
        "cue_enabled": False,
        "stem_eq_mode": False,
    }
    base.update(extra)
    return base


def test_blend_centre_crossfader_halves_both_sides() -> None:
    """[if] blend at centre crossfader does not yield 0.5 per deck on A and B [then] fail."""
    channels = [_ch(1, 1.0, "A"), _ch(2, 1.0, "B")]
    assert deck_weights(channels, 0.5, None, "blend") == {1: pytest.approx(0.5), 2: pytest.approx(0.5)}


def test_upfader_only_ignores_hard_a_crossfader() -> None:
    """[if] upfader_only with crossfader hard A changes deck B weight [then] fail."""
    channels = [_ch(1, 1.0, "A"), _ch(2, 0.7, "B")]
    weights = deck_weights(channels, 0.0, None, "upfader_only")
    assert weights[2] == pytest.approx(0.7)
    assert weights[1] == pytest.approx(1.0)


@pytest.mark.parametrize("master_deck", [1, 2, 3, 4])
def test_master_only_gives_exactly_one_deck_full_weight(master_deck: int) -> None:
    """[if] master_only does not put 1.0 on the master deck only [then] fail."""
    channels = [
        _ch(1, 0.3, "A"),
        _ch(2, 0.9, "B"),
        _ch(3, 0.1, "THRU"),
        _ch(4, 1.0, "A"),
    ]
    weights = deck_weights(channels, 0.5, master_deck, "master_only")
    assert weights[master_deck] == pytest.approx(1.0)
    assert sum(1 for w in weights.values() if w == 1.0) == 1
    for deck_id, weight in weights.items():
        if deck_id != master_deck:
            assert weight == pytest.approx(0.0)


@pytest.mark.parametrize("crossfader", [0.0, 0.25, 0.5, 1.0])
@pytest.mark.parametrize("assign", ["THRU", "thru"])
def test_thru_deck_is_never_scaled_by_the_crossfader(crossfader: float, assign: str) -> None:
    """[if] THRU blend weight varies with crossfader [then] fail."""
    channels = [_ch(1, 0.8, assign)]
    assert deck_weights(channels, crossfader, None, "blend")[1] == pytest.approx(0.8)


def test_unknown_mode_names_allowed_modes() -> None:
    """[if] unknown mode error omits allowed mode names [then] fail."""
    with pytest.raises(ValueError, match="blend") as exc:
        deck_weights([], 0.5, None, "not-a-mode")
    msg = str(exc.value)
    assert "upfader_only" in msg
    assert "master_only" in msg
    assert "not-a-mode" in msg


def test_blend_hard_a_and_hard_b() -> None:
    """[if] hard A or B crossfader does not zero the opposite bus [then] fail."""
    channels = [_ch(1, 1.0, "A"), _ch(2, 1.0, "B")]
    hard_a = deck_weights(channels, 0.0, None, "blend")
    assert hard_a == {1: pytest.approx(1.0), 2: pytest.approx(0.0)}
    hard_b = deck_weights(channels, 1.0, None, "blend")
    assert hard_b == {1: pytest.approx(0.0), 2: pytest.approx(1.0)}


def test_blend_scales_with_fader() -> None:
    """[if] blend does not multiply fader and side gain [then] fail."""
    channels = [_ch(1, 0.5, "A")]
    assert deck_weights(channels, 0.25, None, "blend")[1] == pytest.approx(0.375)


def test_deck_weight_mode_enum_accepted() -> None:
    """[if] DeckWeightMode enum is rejected [then] fail."""
    channels = [_ch(1, 1.0, "A")]
    assert deck_weights(channels, 0.5, None, DeckWeightMode.BLEND) == {1: pytest.approx(0.5)}


def test_master_only_requires_master_deck() -> None:
    """[if] master_only accepts missing master_deck [then] fail."""
    channels = [_ch(1, 1.0, "A")]
    with pytest.raises(ValueError, match="master_deck"):
        deck_weights(channels, 0.5, None, "master_only")
    with pytest.raises(ValueError, match="master_deck"):
        deck_weights(channels, 0.5, 9, "master_only")


def test_blend_ignores_master_deck_none() -> None:
    """[if] blend rejects master_deck None [then] fail."""
    channels = [_ch(1, 1.0, "A")]
    assert deck_weights(channels, 0.0, None, "blend") == {1: pytest.approx(1.0)}


def test_empty_channels_blend_returns_empty() -> None:
    """[if] empty channels in blend is not {} [then] fail."""
    assert deck_weights([], 0.5, None, "blend") == {}


@pytest.mark.parametrize(
    ("crossfader", "fragment"),
    [(1.5, "crossfader"), (float("nan"), "crossfader")],
)
def test_invalid_crossfader_raises(crossfader: float, fragment: str) -> None:
    """[if] out-of-range crossfader is accepted [then] fail."""
    channels = [_ch(1, 1.0, "A")]
    with pytest.raises(ValueError, match=fragment):
        deck_weights(channels, crossfader, None, "blend")


def test_invalid_fader_raises() -> None:
    """[if] negative fader is accepted [then] fail."""
    with pytest.raises(ValueError, match="fader"):
        deck_weights([_ch(1, -0.1, "A")], 0.5, None, "blend")


def test_invalid_assign_raises() -> None:
    """[if] unknown assign is accepted [then] fail."""
    with pytest.raises(ValueError, match="assign"):
        deck_weights([_ch(1, 1.0, "C")], 0.5, None, "blend")


def test_duplicate_deck_id_raises() -> None:
    """[if] duplicate deck_id is accepted [then] fail."""
    channels = [_ch(1, 1.0, "A"), _ch(1, 0.5, "B")]
    with pytest.raises(ValueError, match="duplicate"):
        deck_weights(channels, 0.5, None, "blend")


def test_missing_fader_raises() -> None:
    """[if] missing fader key is accepted [then] fail."""
    bad = _ch(1, 1.0, "A")
    del bad["fader"]
    with pytest.raises(ValueError, match="fader"):
        deck_weights([bad], 0.5, None, "blend")


def test_crossfader_gain_linear() -> None:
    """[if] crossfader_gain is not linear per assign [then] fail."""
    assert crossfader_gain("A", 0.25) == pytest.approx(0.75)
    assert crossfader_gain("B", 0.25) == pytest.approx(0.25)
    assert crossfader_gain("THRU", 0.0) == pytest.approx(1.0)
    assert crossfader_gain("thru", 1.0) == pytest.approx(1.0)
