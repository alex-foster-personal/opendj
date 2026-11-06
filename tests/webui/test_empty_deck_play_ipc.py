"""DECKUX-4985: empty-deck play must refuse in IPC, not via engine Sentry throw.

[if] play hits an empty deck [then] IPC refuses before engine throw, [else stop].
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("DECKUX-4985")

FRONTEND = Path(__file__).resolve().parents[2] / "apps" / "webui" / "frontend"


def test_ipc_refuses_empty_deck_play_before_engine_throw() -> None:
    """[if] play hits an empty deck [then] IPC refuses before engine throw, [else stop]."""
    ipc = (FRONTEND / "src/lib/rb/performance-ipc.svelte.ts").read_text(encoding="utf-8")
    assert "function _refuseEmptyDeckTransport" in ipc
    assert "_refuseEmptyDeckTransport(command.deck, command.type === 'cue' ? 'cueing' : 'play')" in ipc
    assert "OPEN-DJ-BE-6Y" in ipc
    engine = (FRONTEND / "src/lib/rb/audio-engine.svelte.ts").read_text(encoding="utf-8")
    assert "${op}: no track loaded on deck ${deck}" in engine
