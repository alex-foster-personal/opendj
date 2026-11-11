"""Issue #11323 acceptance wiring for SET OUTPUTS hover, alert, stack, and Rescan (IOPIN-11323)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CLUSTER = (REPO / "apps/webui/frontend/src/lib/components/rb/mixer/HeadphoneCluster.svelte").read_text(encoding="utf-8")
EXPLAINER = (REPO / "apps/webui/frontend/src/lib/components/rb/deck/ControlExplainer.svelte").read_text(
    encoding="utf-8"
)


def _io_explainer_tag() -> str:
    start = CLUSTER.index('title="Audio I/O quick settings"')
    open_tag = CLUSTER.rindex("<ControlExplainer", 0, start)
    close = CLUSTER.index(">", start)
    return CLUSTER[open_tag : close + 1]


def _io_panel_block() -> str:
    start = CLUSTER.index("{#if ioSurface.open}")
    end = CLUSTER.index("{#snippet outputMenu()}", start)
    return CLUSTER[start:end]


def _markup_outside_panel() -> str:
    markup = CLUSTER[CLUSTER.index("</script>") : CLUSTER.index("<style>")]
    return markup.replace(_io_panel_block(), "")


@pytest.mark.requirement("IOPIN-11323")
def test_pointer_leave_uses_instant_dismiss_not_stay_popover() -> None:
    """[if] pointer leaves SET OUTPUTS without a stay popover [then] I/O hover is instant, [else stop]."""
    tag = _io_explainer_tag()
    assert 'dismiss="instant"' in tag
    assert "pinOnClick" not in tag
    assert "event instanceof PointerEvent && dismissMode === 'stay'" in EXPLAINER


@pytest.mark.requirement("IOPIN-11323")
def test_hover_is_compact_sentence_devices_and_cta_only() -> None:
    """[if] SET OUTPUTS hover is showing [then] copy is compact routing lines only, [else stop]."""
    tag = _io_explainer_tag()
    assert "ioSummary.sentence" in CLUSTER and "ioSummary.cta" in CLUSTER
    assert "compact={true}" in tag
    assert re.search(r"\baction=", tag) is None


@pytest.mark.requirement("IOPIN-11323")
def test_unset_outputs_expose_io_alert_on_set_outputs() -> None:
    """[if] outputs are unset and SET OUTPUTS not clicked [then] io-alert is wired on the trigger, [else stop]."""
    assert "class:io-alert={ioAlert}" in CLUSTER
    assert "data-io-alert={ioAlert ? 'unset' : undefined}" in CLUSTER


@pytest.mark.requirement("IOPIN-11323")
def test_main_output_picker_stacks_above_headphone_cue_in_panel() -> None:
    """[if] Audio I/O panel is open [then] MAIN picker precedes CUE in outputMenu, [else stop]."""
    start = CLUSTER.index("{#snippet outputMenu()}")
    end = CLUSTER.index("{/snippet}", start)
    menu = CLUSTER[start:end]
    master = menu.index('aria-label="master output device"')
    cue = menu.index('aria-label="headphone output device"')
    assert 0 < master < cue


@pytest.mark.requirement("IOPIN-11323")
def test_rescan_is_absent_from_closed_hp_row() -> None:
    """[if] I/O panel is closed on performance [then] the .hp row has no Rescan control, [else stop]."""
    outside = _markup_outside_panel()
    assert 'aria-label="Rescan available headphone output devices"' not in outside
