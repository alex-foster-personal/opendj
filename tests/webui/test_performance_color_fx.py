"""Color FX dispatcher/read-model wiring guards for issue #12208.

[if] Color FX rejects [then] the deck exposes settled failure and recovery, [else stop].

Execution evidence is in frontend tests/unit/color-fx.test.mjs and
color-fx-strip-render.test.mjs: real dispatcher refusal, rollback and retry,
plus actual strip and deck-banner rendering. These guards require no Node runtime.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("COLORFX-12718")

FRONTEND = Path(__file__).resolve().parents[2] / "apps/webui/frontend/src/lib"


def test_color_fx_settlement_uses_shared_deck_status() -> None:
    """[if] Color FX rejects [then] shared deck error and pending state are exposed, [else stop]."""
    dispatcher = (FRONTEND / "rb/performance-ipc.svelte.ts").read_text(encoding="utf-8")
    branch = dispatcher.split("} else if (command.type === 'color_fx') {", 1)[1].split(
        "} else if (command.type === 'autoplay')", 1
    )[0]
    assert "await engine.setFilter(" in branch
    # PR #10647 loops over each targeted deck (deck all), so the restore names the loop deck.
    assert "restoreColorFx(one.deck, acknowledged)" in branch
    assert "command_error: performanceCommandStatus.deck_errors[deckId]" in dispatcher
    assert "command_pending: performanceCommandStatus.deck_pending[deckId] > 0" in dispatcher


def test_color_fx_strip_and_banner_share_the_deck_read_model() -> None:
    """[if] deck command status changes [then] Color FX and its banner use that status, [else stop]."""
    mixer = (FRONTEND / "components/rb/Mixer.svelte").read_text(encoding="utf-8")
    strip = (FRONTEND / "components/rb/mixer/ChannelStrip.svelte").read_text(encoding="utf-8")
    deck = (FRONTEND / "components/rb/Deck.svelte").read_text(encoding="utf-8")
    assert "commandPending={performanceCommandStatus.deck_pending[deck] > 0}" in mixer
    assert "commandError={performanceCommandStatus.deck_errors[deck]}" in mixer
    assert "aria-busy={commandPending}" in strip
    assert "if (commandError !== null) return commandError;" in strip
    assert "performanceCommandStatus.deck_errors[deckId]" in deck
    assert "<DeckErrorBanner" in deck
    assert "runPerformanceCommandFromUi({ type: 'color_fx', deck, effect })" in mixer
