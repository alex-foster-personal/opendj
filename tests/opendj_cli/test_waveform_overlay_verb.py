"""set_waveform_overlay CLI domain includes cue-marker overlays (WAVEQS-11730).

[if] the CLI refuses a cue-marker overlay id [then] agents cannot drive toggles, [else stop].
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.opendj_cli.catalog import WAVEFORM_OVERLAY_VALUES
from apps.opendj_cli.verbs import InvocationError, parse_invocation

pytestmark = pytest.mark.requirement("WAVEQS-11730")

_NEW_OVERLAYS = ("hot_cues", "memory_cues", "loop_cues")

_TS_OVERLAYS = Path(__file__).resolve().parents[2] / "apps/webui/frontend/src/lib/rb/waveform-overlay-command.ts"


def test_cli_overlay_values_match_typescript() -> None:
    """[if] CLI and TS overlay lists drift [then] agent parity lies, [else stop]."""
    src = _TS_OVERLAYS.read_text()
    bool_match = re.search(r"WAVEFORM_BOOL_OVERLAYS\s*=\s*\[([\s\S]*?)\]\s*as const", src)
    assert bool_match is not None
    assert "grid_lines" in src
    ts_values = re.findall(r"'([^']+)'", bool_match.group(1)) + ["grid_lines"]
    assert list(WAVEFORM_OVERLAY_VALUES) == ts_values


@pytest.mark.parametrize("name", _NEW_OVERLAYS)
def test_set_waveform_overlay_parses_cue_marker_overlays(name: str) -> None:
    """[if] the CLI refuses a shipped overlay id [then] agents cannot drive the toggle, [else stop]."""
    assert parse_invocation(["set_waveform_overlay", name, "false"]).command == {
        "type": "set_waveform_overlay",
        "overlay": name,
        "enabled": False,
    }
    assert parse_invocation(["set_waveform_overlay", f"overlay={name}", "enabled=true"]).command == {
        "type": "set_waveform_overlay",
        "overlay": name,
        "enabled": True,
    }


def test_set_waveform_overlay_rejects_unknown_overlay() -> None:
    """[if] a bogus overlay is accepted [then] the domain is meaningless, [else stop]."""
    with pytest.raises(InvocationError, match="overlay"):
        parse_invocation(["set_waveform_overlay", "bogus", "false"])
