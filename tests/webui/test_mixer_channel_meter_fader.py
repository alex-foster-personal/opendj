"""Mixer channel meter follows the channel fader (issue #11321, pin 3dc62dbb909c).

[if] the channel bar uses heardDb [then] the fader cannot move orange/red segments, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_LEVEL_METER = REPO_ROOT / "apps/webui/frontend/src/lib/components/rb/mixer/ChannelLevelMeter.svelte"
DECK_CHANNEL_GRAPH = REPO_ROOT / "apps/webui/frontend/src/lib/rb/deck-channel-graph.ts"
GRAPH_CORE = REPO_ROOT / "apps/webui/frontend/src/lib/rb/deck-channel-graph-core.ts"
METER_ARTIFACT = REPO_ROOT / "apps/webui/frontend/tests/e2e/meter-artifact.spec.ts"
CHANNEL_HARNESS = REPO_ROOT / "apps/webui/frontend/tests/e2e/fixtures/channel-meter-browser-entry.ts"

pytestmark = pytest.mark.requirement("METERCAL-05")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_channel_meter_bar_uses_post_fader_db() -> None:
    """[if] segments ignore post-fader db [then] fader moves cannot change orange/red magnitude, [else stop]."""
    src = _read(CHANNEL_LEVEL_METER)
    assert "segmentsLitFromDbfs(channelDb, thresholds)" in src
    assert "db = channelDb" in src
    assert "segmentsLitFromDbfs(heardDb" not in src


def test_deck_graph_taps_the_channel_fader_node() -> None:
    """[if] the tap is not on fader [then] the slider and meter disagree, [else stop]."""
    graph = _read(GRAPH_CORE)
    assert "meterSources.push({ tap, source: fader" in graph
    assert "source: high" not in graph


def test_meter_artifact_covers_closed_fader_then_unity() -> None:
    """[if] unity restore is untested [then] post-fader rise after close is unproven, [else stop]."""
    spec = _read(METER_ARTIFACT)
    assert "readChannelMeterAtFaderGains([1, 0, 1])" in spec
    assert "unity fader must rise again on the post-fader tap" in spec


def test_browser_harness_drives_production_fader_gain() -> None:
    """[if] the harness skips fader.gain [then] graph proof is fake, [else stop]."""
    harness = _read(CHANNEL_HARNESS)
    assert "buildDeckChannelGraphCore" in harness
    assert "deckNodes.fader.gain.setValueAtTime(value, ctx.currentTime)" in harness
    assert "readChannelMeterAtFaderGains" in harness
    assert "meterSources.push({ tap, source: fader" in _read(GRAPH_CORE)


def test_harness_and_production_share_one_channel_graph_builder() -> None:
    """[if] production or the harness builds its own channel graph [then] the harness stops covering production wiring (#12665), [else stop]."""
    production = _read(DECK_CHANNEL_GRAPH)
    assert "return buildDeckChannelGraphCore({" in production
    assert "createGain(" not in production
    assert "meterSources.push(" not in production
    harness = _read(CHANNEL_HARNESS)
    assert "from '$lib/rb/deck-channel-graph-core'" in harness
    assert "createBiquadFilter(" not in harness
    assert "meterSources.push({ tap, source: fader" not in harness
    assert not (REPO_ROOT / "apps/webui/frontend/src/lib/rb/deck-channel-meter-harness-graph.ts").exists()


def test_next_observation_floors_when_fader_closes() -> None:
    """[if] close fader omits silent observation wait [then] floor timing is wrong, [else stop]."""
    harness = _read(CHANNEL_HARNESS)
    assert "waitForSilentObservationAfter" in harness
    spec = _read(METER_ARTIFACT)
    assert "floors on the next observation when the channel fader closes" in spec
