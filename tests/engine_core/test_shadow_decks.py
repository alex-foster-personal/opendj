"""Hidden shadow engine decks 5-8 (NAE-11978, issue #11153).

[if] a snapshot is taken with no shadow load [then] cargo exposes eight empty slots, [else stop].
[if] play/load/seek names deck 5-8 [then] cargo accepts them inaudible on master, [else stop].
[if] the mixer or wavestack renders [then] Python wire types keep live ids 1-4 only, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sets.sources import opendj_wire
from apps.webui.server import models
from tests.rust_build_env import run_cargo_lib_tests

pytestmark = pytest.mark.requirement("NAE-11978")

AUDIO_ENGINE = Path(__file__).resolve().parents[2] / "apps" / "audio-engine"


def _cargo_test(name: str) -> None:
    # --exact matches the full test path, so name the module the test lives in.
    done = run_cargo_lib_tests(AUDIO_ENGINE, (f"engine::tests::{name}",), exact=True)
    assert done.returncode == 0, done.stderr[-4000:]
    assert f"engine::tests::{name} ... ok" in done.stdout, done.stdout[-4000:]


@pytest.mark.requirement("NAE-11978")
def test_snapshot_exposes_eight_decks_with_empty_shadow_slots() -> None:
    """[if] a snapshot is taken with no shadow load [then] decks 5-8 are empty, [else stop]."""
    _cargo_test("snapshot_exposes_eight_decks_with_empty_shadow_slots")


@pytest.mark.requirement("NAE-11978")
def test_shadow_deck_commands_apply_on_protocol() -> None:
    """[if] play/load/seek names deck 5, 6, 7, or 8 [then] protocol accepts them, [else stop]."""
    _cargo_test("shadow_deck_commands_apply_but_stay_inaudible_on_the_master")


@pytest.mark.requirement("NAE-11978")
def test_models_keep_mixer_on_live_decks_only() -> None:
    """[if] the mixer or wavestack renders [then] only live deck ids 1-4 are exposed, [else stop]."""
    assert models.LIVE_DECK_IDS == (1, 2, 3, 4)
    assert models.ENGINE_DECK_IDS == (1, 2, 3, 4, 5, 6, 7, 8)
    assert opendj_wire.KNOWN_DECK_IDS == frozenset({"1", "2", "3", "4"})
    assert opendj_wire.ENGINE_DECK_IDS == frozenset(str(i) for i in range(1, 9))
