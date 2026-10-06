"""PLAY-18 CLI parity: an AutoPlay off carries user provenance only when asked."""

import pytest

from apps.opendj_cli.verbs import parse_invocation


# REQ: PLAY-18
@pytest.mark.requirement("PLAY-18")
def test_autoplay_off_without_by_user_sends_no_provenance() -> None:
    """[if] the CLI sends autoplay false [then] no by_user key is sent, [else stop]."""
    assert parse_invocation(["autoplay", "false"]).command == {"type": "autoplay", "enabled": False}


# REQ: PLAY-18
@pytest.mark.requirement("PLAY-18")
def test_autoplay_off_with_by_user_sends_user_provenance() -> None:
    """[if] the CLI sends autoplay false true [then] by_user is true, [else stop]."""
    command = parse_invocation(["autoplay", "false", "true"]).command
    assert command == {"type": "autoplay", "enabled": False, "by_user": True}
