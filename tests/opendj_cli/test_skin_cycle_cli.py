"""CHROME-12 agent parity: every skin the top-bar button cycles is reachable by CLI.

[if] ``opendj set_skin light ...`` is rejected [then ⛔] an agent cannot reach
     the light skin the button can.
[if] the CLI skin enum disagrees with the webui cycle order [then ⛔] one
     surface can set a skin the other cannot.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.opendj_cli.catalog import LAYOUT_PRESET_VALUES, UI_SKIN_VALUES
from apps.opendj_cli.verbs import InvocationError, parse_invocation

_REPO = Path(__file__).resolve().parents[2]
_SKIN_LAYOUT_TS = _REPO / "apps/webui/frontend/src/lib/rb/skin-layout.ts"


@pytest.mark.requirement("CHROME-12")
@pytest.mark.parametrize("skin", ["default", "mono-dev", "light", "green-room", "practice-ddj-flx4"])
def test_set_skin_reaches_every_cycled_skin(skin: str) -> None:
    """[if] set_skin <skin> builds no set_skin command [then] the cycle is UI-only, [else stop]."""
    assert parse_invocation(["set_skin", skin, "rekordbox", "auto"]).command == {
        "type": "set_skin", "ui_skin": skin, "wave_palette": "rekordbox", "wave_split_master": "auto",
    }


@pytest.mark.requirement("CHROME-12")
def test_cli_skin_values_match_the_webui_cycle_order() -> None:
    """[if] UI_SKIN_VALUES is not default, mono-dev, light, green-room, practice-ddj-flx4 [then] CLI and button disagree, [else stop]."""
    assert UI_SKIN_VALUES == ("default", "mono-dev", "light", "green-room", "practice-ddj-flx4")


@pytest.mark.requirement("CHROME-12")
def test_set_skin_rejects_an_unknown_skin() -> None:
    """[if] set_skin neon parses [then] the enum is not enforced, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["set_skin", "neon", "rekordbox", "auto"])


@pytest.mark.requirement("CHROME-15")
def test_set_waveform_design_accepts_auto() -> None:
    """[if] set_waveform_design auto is rejected [then] an agent cannot restore follow-the-skin, [else stop]."""
    assert parse_invocation(["set_waveform_design", "auto"]).command == {
        "type": "set_waveform_design", "design": "auto",
    }


@pytest.mark.requirement("CHROME-15")
def test_set_skin_accepts_an_auto_palette() -> None:
    """[if] set_skin mono-dev auto auto is rejected [then] the CLI cannot reach the default look, [else stop]."""
    assert parse_invocation(["set_skin", "mono-dev", "auto", "auto"]).command == {
        "type": "set_skin", "ui_skin": "mono-dev", "wave_palette": "auto", "wave_split_master": "auto",
    }


@pytest.mark.requirement("SKINLAYOUT-10199")
@pytest.mark.parametrize(
    ("surface", "preset"),
    [
        ("performance", "deck-grid"),
        ("library", "three-panel"),
        ("library", "auto"),
    ],
)
def test_set_layout_preset_builds_command(surface: str, preset: str) -> None:
    """[if] set_layout_preset does not round-trip [then] agents cannot set layout overrides, [else stop]."""
    assert parse_invocation(["set_layout_preset", surface, preset]).command == {
        "type": "set_layout_preset",
        "surface": surface,
        "preset": preset,
    }


@pytest.mark.requirement("SKINLAYOUT-10199")
def test_set_layout_preset_rejects_unknown_tokens() -> None:
    """[if] unknown surface or preset tokens parse [then] the enum is not enforced, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["set_layout_preset", "performance", "neon"])
    with pytest.raises(InvocationError):
        parse_invocation(["set_layout_preset", "mixer", "auto"])


@pytest.mark.requirement("SKINLAYOUT-10199")
def test_layout_preset_values_match_typescript_catalog() -> None:
    """[if] LAYOUT_PRESET_VALUES disagrees with skin-layout.ts [then] CLI and bus catalogs diverge, [else stop]."""
    text = _SKIN_LAYOUT_TS.read_text(encoding="utf-8")
    perf_m = re.search(
        r"export const PERFORMANCE_LAYOUTS: readonly PerformanceLayout\[\] = (\[[^\]]+\])",
        text,
    )
    lib_m = re.search(
        r"export const LIBRARY_LAYOUTS: readonly LibraryLayout\[\] = (\[[^\]]+\])",
        text,
    )
    assert perf_m and lib_m
    perf = tuple(re.findall(r"'([^']+)'", perf_m.group(1)))
    lib = tuple(re.findall(r"'([^']+)'", lib_m.group(1)))
    assert ("auto", *perf, *lib) == LAYOUT_PRESET_VALUES
