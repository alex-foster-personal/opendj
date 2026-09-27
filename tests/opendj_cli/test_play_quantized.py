"""LATENCY-02 CLI parity for QUANTIZED LAUNCH."""

import pytest

from apps.opendj_cli.verbs import parse_invocation


# REQ: LATENCY-02
@pytest.mark.requirement("LATENCY-02")
def test_play_emits_no_quantize_key() -> None:
    """[if] the CLI sends a plain play [then] the command has no quantize key, [else stop]."""
    command = parse_invocation(["play", "1"]).command
    assert command == {"type": "play", "deck": 1, "playing": True}
    assert "quantize" not in command


# REQ: LATENCY-02
@pytest.mark.requirement("LATENCY-02")
def test_play_quantized_emits_quantize_true() -> None:
    """[if] the CLI sends play_quantized [then] the command has quantize true, [else stop]."""
    command = parse_invocation(["play_quantized", "1"]).command
    assert command == {"type": "play", "deck": 1, "playing": True, "quantize": True}
