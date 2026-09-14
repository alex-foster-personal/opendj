"""LATENCY-02 CLI parity for QUANTIZED LAUNCH."""

from apps.opendj_cli.verbs import parse_invocation


def test_play_emits_no_quantize_key() -> None:
    command = parse_invocation(["play", "1"]).command
    assert command == {"type": "play", "deck": 1, "playing": True}
    assert "quantize" not in command


def test_play_quantized_emits_quantize_true() -> None:
    command = parse_invocation(["play_quantized", "1"]).command
    assert command == {"type": "play", "deck": 1, "playing": True, "quantize": True}
