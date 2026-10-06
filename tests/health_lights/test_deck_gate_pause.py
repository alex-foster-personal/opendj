"""The drains pause while a deck PLAYS, not while a deck is loaded (DRAIN-PAUSE-01).

Build 16's verify saw the drain report ``paused_playing`` with decks loaded and
stopped, resuming only after an unload. A DJ almost always has decks loaded, so
that is background analysis that never runs. Both directions are pinned here:
loaded-and-stopped runs, and playing pauses (the overshoot control, so the fix
cannot be "never pause").

Regression lines:
  - if loaded, stopped decks pause the drain then analysis never runs in real use
  - if a deck id dropping out of one mirror snapshot restarts the settle hold then the same
  - if a playing deck does not pause the drain then analysis competes with playback
  - if a new track landing does not hold for LOAD_SETTLE_S then the drain races the deck load
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apps.webui.server import coverage_drain as cd
from apps.webui.server import coverage_outcomes as co

pytestmark = pytest.mark.requirement("HEALTH-06")

SETTLED = cd.LOAD_SETTLE_S + 1


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Measured(Exception):
    """The drain got past its pause check and reached its snapshot."""


def _mirror(*decks: tuple[str | None, bool]) -> dict[str, Any]:
    return {"decks": {str(n): {"stable_id": sid, "playing": playing} for n, (sid, playing) in enumerate(decks, 1)}}


def _loaded_gate() -> tuple[cd.DeckGate, Clock, dict[str, Any]]:
    """Four decks loaded and stopped, past the load settle."""
    clock = Clock()
    box = {"mirror": _mirror(("a", False), ("b", False), ("c", False), ("d", False))}
    gate = cd.DeckGate(lambda: box["mirror"], clock=clock)
    assert gate() is True  # the loads themselves hold
    clock.now += SETTLED
    return gate, clock, box


def _drain(tmp_path: Path, gate: cd.DeckGate) -> cd.CoverageDrain:
    def snapshot() -> Any:
        raise Measured

    return cd.CoverageDrain(
        snapshot_fn=snapshot,
        jobs={},
        playing_fn=gate,
        outcomes=co.OutcomeStore(co.store_path(tmp_path)),
        config=cd.DrainConfig(cd.config_path(tmp_path)),
    )


#-----------------------------------------------------------------------------
# loaded and stopped RUNS
#-----------------------------------------------------------------------------
def test_loaded_and_stopped_decks_do_not_hold_the_gate() -> None:
    """[if] four decks are loaded and stopped past the settle [then] no hold, [else stop]."""
    gate, clock, _ = _loaded_gate()
    for _ in range(10):
        assert gate() is False
        clock.now += 5


def test_an_id_that_blinks_out_of_one_snapshot_is_not_a_new_load() -> None:
    """[if] a deck id drops out of one snapshot and returns [then] no hold, [else stop]."""
    gate, _, box = _loaded_gate()
    loaded = box["mirror"]
    box["mirror"] = _mirror((None, False), (None, False), ("c", False), ("d", False))
    assert gate() is False
    box["mirror"] = None  # the page closed and reopened
    assert gate() is False
    box["mirror"] = loaded
    assert gate() is False, "a deck that still holds its track was read as a new load"


def test_the_drain_works_with_decks_loaded_and_stopped(tmp_path: Path) -> None:
    """[if] decks are loaded and stopped [then] the drain tick is not paused_playing, [else stop]."""
    gate, _, _ = _loaded_gate()
    with pytest.raises(Measured):
        _drain(tmp_path, gate).tick()


#-----------------------------------------------------------------------------
# playing PAUSES, and a new load still holds (overshoot controls)
#-----------------------------------------------------------------------------
def test_a_playing_deck_holds_the_gate_long_after_its_load() -> None:
    """[if] one loaded deck plays, long after its load [then] the gate holds, [else stop]."""
    gate, clock, box = _loaded_gate()
    box["mirror"] = _mirror(("a", True), ("b", False), ("c", False), ("d", False))
    clock.now += 3_600
    assert gate() is True
    box["mirror"] = _mirror(("a", False), ("b", False), ("c", False), ("d", False))
    assert gate() is False


def test_the_drain_pauses_while_a_deck_plays(tmp_path: Path) -> None:
    """[if] a deck plays [then] the drain tick is paused_playing, with no snapshot, [else stop]."""
    gate, _, box = _loaded_gate()
    box["mirror"] = _mirror(("a", False), ("b", True), ("c", False), ("d", False))
    assert _drain(tmp_path, gate).tick() == "paused_playing"


def test_a_new_track_on_a_loaded_deck_holds_for_the_settle() -> None:
    """[if] a deck takes a different track [then] hold LOAD_SETTLE_S, then release, [else stop]."""
    gate, clock, box = _loaded_gate()
    box["mirror"] = _mirror(("e", False), ("b", False), ("c", False), ("d", False))
    assert gate() is True
    clock.now += cd.LOAD_SETTLE_S - 1
    assert gate() is True
    clock.now += 2
    assert gate() is False
