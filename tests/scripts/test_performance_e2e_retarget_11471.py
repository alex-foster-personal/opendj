"""Issue #11471: performance e2e specs target visible controls by role, not stale CSS."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXPLAINERS = REPO / "apps/webui/frontend/tests/e2e/performance-issue-3982-explainers.spec.ts"
PREVIEW_SYNC = REPO / "apps/webui/frontend/tests/e2e/performance-preview-beat-sync.spec.ts"
DECK_HOVER = REPO / "apps/webui/frontend/tests/e2e/performance-deck-hover-correspondence.spec.ts"
LAYOUT_HELPER = REPO / "apps/webui/frontend/tests/e2e/support/performance-visible-layout.ts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.requirement("E2E-12837")
def test_explainers_avoid_hidden_chrome_and_use_roles() -> None:
    """[if] explainers hover hidden chrome [then] specs fail, [else stop]."""
    src = _read(EXPLAINERS)
    assert "ensureMoreDeckLayout" in src
    assert "ensureWideViewportForTopBarChrome" in src
    assert "openAudioIoPanel" in src
    assert "visibleHeadphoneOutputMode" in src
    assert "getByRole('button', { name: 'FX panel' })" in src
    assert "visibleHeadphoneMix" in src
    assert ".hp-mode" not in src.replace("visibleHeadphoneOutputMode", "")


@pytest.mark.requirement("E2E-12837")
def test_preview_beat_sync_settings_use_accessible_label() -> None:
    """[if] preview beat-sync uses a stale title selector [then] settings miss, [else stop]."""
    src = _read(PREVIEW_SYNC)
    assert "previewBeatSyncSelect" in src
    assert 'select[title="Tempo-match' not in src
    assert "Meta+Comma" not in src
    assert "ControlOrMeta+Comma" in _read(LAYOUT_HELPER)


@pytest.mark.requirement("E2E-12837")
def test_preview_beat_sync_keeps_rate_and_snap_assertions() -> None:
    """[if] preview beat-sync drops tempo or snap checks [then] regression, [else stop]."""
    src = _read(PREVIEW_SYNC)
    for needle in (
        "toBeCloseTo(1.05",
        "outOfRange",
        "switchedOff",
        "snapped.snap",
        "phase",
        "tempo",
    ):
        assert needle in src


@pytest.mark.requirement("E2E-12837")
def test_deck_hover_targets_visible_deck_and_mixer() -> None:
    """[if] deck-hover skips mixer or deck selectors [then] correspondence drifts, [else stop]."""
    src = _read(DECK_HOVER)
    assert "visibleDeckOne" in src
    assert "visibleMixerChannelOne" in src
    assert "ensureMoreDeckLayout" in src
    assert "locator('[data-mixer-channel=\"1\"]')" not in src
    assert 'locator(".rb-deck[data-deck-hover="1"]")' not in src
