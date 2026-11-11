"""BROWSER-12636: Cmd/Ctrl+A selects every visible library track (pin 0b1e12cc01d0).

[if] TrackTable and BrowserPanel wire select_all [then] focused rows call selectAllRows, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("BROWSER-12636")

REPO = Path(__file__).resolve().parents[2]
TABLE = REPO / "apps/webui/frontend/src/lib/components/rb/browser/TrackTable.svelte"
PANEL = REPO / "apps/webui/frontend/src/lib/components/rb/BrowserPanel.svelte"
KEYS = REPO / "apps/webui/frontend/src/lib/components/rb/browser/library-edit-keys.ts"


def test_library_edit_keys_runs_select_all_before_lazy_clipboard_import() -> None:
    """[if] window Cmd+A [then] selectAllRows runs without awaiting clipboard, [else stop]."""
    src = KEYS.read_text(encoding="utf-8")
    assert "import { selectAllRows, type RowRef } from './pane-row-selection';" in src
    assert "if (action === 'select_all')" in src
    assert "void _runLibraryEdit(action" in src


def test_track_table_forwards_cmd_a_to_onselectalltracks() -> None:
    """[if] Cmd+A on a focused track row [then] TrackTable calls onselectalltracks, [else stop]."""
    src = TABLE.read_text(encoding="utf-8")
    assert "action.kind === 'select_all'" in src
    assert "onselectalltracks?.()" in src


def test_browser_panel_passes_onselectalltracks() -> None:
    """[if] the track list is shown [then] BrowserPanel wires onselectalltracks to selectAllRows, [else stop]."""
    src = PANEL.read_text(encoding="utf-8")
    assert "onselectalltracks=" in src
    assert "selectAllRows(pane, renderedRows)" in src
