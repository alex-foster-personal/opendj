"""DECKUX-39 CLI parity: the quantize verb carries user provenance only when asked."""

import pytest

from apps.opendj_cli.verbs import parse_invocation


# REQ: DECKUX-39
@pytest.mark.requirement("DECKUX-39")
def test_quantize_without_by_user_emits_no_provenance() -> None:
    """[if] the CLI sends quantize 1 false [then] no by_user key is sent, [else stop]."""
    command = parse_invocation(["quantize", "1", "false"]).command
    assert command == {"type": "quantize", "deck": 1, "enabled": False}


# REQ: DECKUX-39
@pytest.mark.requirement("DECKUX-39")
def test_quantize_with_by_user_emits_user_provenance() -> None:
    """[if] the CLI sends quantize 1 false true [then] by_user is true, [else stop]."""
    command = parse_invocation(["quantize", "1", "false", "true"]).command
    assert command == {"type": "quantize", "deck": 1, "enabled": False, "by_user": True}
