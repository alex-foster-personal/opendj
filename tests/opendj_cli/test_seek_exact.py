"""Cmd K v2 CLI parity: `seek` carries the exact flag only when the caller asks for it."""

from apps.opendj_cli.verbs import parse_invocation


def test_seek_without_exact_takes_the_quantized_path() -> None:
    """[if] the CLI sends seek 1 1000 [then] no exact key is sent, [else stop]."""
    assert parse_invocation(["seek", "1", "1000"]).command == {
        "type": "seek",
        "deck": 1,
        "position_ms": 1000,
    }


def test_seek_with_exact_sends_the_exact_flag() -> None:
    """[if] the CLI sends seek 1 1000 true [then] exact is true, [else stop]."""
    command = parse_invocation(["seek", "1", "1000", "true"]).command
    assert command == {"type": "seek", "deck": 1, "position_ms": 1000, "exact": True}
