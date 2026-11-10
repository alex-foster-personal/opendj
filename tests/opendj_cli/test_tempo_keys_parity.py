"""KEYB-11711 agent parity for tempo step and pitch-range cycle keys."""

import re
from pathlib import Path

import pytest

from apps.opendj_cli.catalog import PITCH_RANGE_VALUES
from apps.opendj_cli.verbs import parse_invocation

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PITCH_RANGES_TS = _REPO_ROOT / "apps/webui/frontend/src/lib/player/constants.ts"


# REQ: KEYB-11711
@pytest.mark.requirement("KEYB-11711")
def test_tempo_cli_matches_ui_ratio_write() -> None:
    """[if] the UI fine step from +1.2% [then] CLI tempo 1.013 matches, [else stop]."""
    command = parse_invocation(["tempo", "2", "1.013"]).command
    assert command == {"type": "tempo", "deck": 2, "ratio": 1.013}


# REQ: KEYB-11711
@pytest.mark.requirement("KEYB-11711")
def test_pitch_range_cli_matches_ui_cycle_write() -> None:
    """[if] the UI cycles from 8% to 16% [then] CLI pitch_range 16 matches, [else stop]."""
    command = parse_invocation(["pitch_range", "2", "16"]).command
    assert command == {"type": "pitch_range", "deck": 2, "range": 16}


# REQ: KEYB-11711
@pytest.mark.requirement("KEYB-11711")
def test_cli_pitch_range_values_match_frontend_constant() -> None:
    """[if] PITCH_RANGES drifts from the CLI enum [then] the P key and agents disagree, [else stop]."""
    text = _PITCH_RANGES_TS.read_text(encoding="utf-8")
    match = re.search(r"PITCH_RANGES\s*:\s*readonly\s+PitchRange\[\]\s*=\s*\[([^\]]+)\]", text)
    assert match is not None, "PITCH_RANGES literal missing in constants.ts"
    frontend_values = tuple(int(part.strip()) for part in match.group(1).split(","))
    assert frontend_values == PITCH_RANGE_VALUES
