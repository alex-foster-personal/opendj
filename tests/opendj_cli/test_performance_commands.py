"""DECKUX-10470 (#13362): CLI validation for the beat jump size and the beat jump verbs.

[if] the CLI gets a whole beat-jump size [then] it reaches the bus as an int, [else stop].
[if] the CLI gets an exact half-beat size [then] it reaches the bus as 0.5, [else stop].
[if] the CLI gets zero or an unsupported fraction [then] it is refused naming the sizes, [else stop].

Catalog-versus-TypeScript drift lives in ``test_bus_parity.py``.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import BEAT_JUMP_SIZE_VALUES
from apps.opendj_cli.verbs import InvocationError, parse_invocation

pytestmark = pytest.mark.requirement("DECKUX-14853")

WHOLE_SIZES: tuple[int, ...] = tuple(int(size) for size in BEAT_JUMP_SIZE_VALUES if float(size).is_integer())


def test_whole_beat_jump_sizes_reach_the_bus_as_ints() -> None:
    """[if] a whole beat jump size such as 4 or 4.0 reaches the bus as a float or is refused [then] parity breaks, [else stop]."""
    assert WHOLE_SIZES == (1, 2, 4, 8, 16, 32, 64)
    for size in WHOLE_SIZES:
        command = parse_invocation(["beat_jump_size", "3", str(size)]).command
        assert command == {"type": "beat_jump_size", "deck": 3, "beats": size}
        assert type(command["beats"]) is int
    assert type(parse_invocation(["beat_jump_size", "1", "4.0"]).command["beats"]) is int
    assert parse_invocation(["beat_jump", "2", "-8"]).command == {"type": "beat_jump", "deck": 2, "beats": -8}


def test_half_beat_jump_size_and_jump_reach_the_bus_as_half() -> None:
    """[if] 0.5 is refused as a beat jump size or +-0.5 as a beat jump [then] the half-beat jump has no CLI, [else stop]."""
    for token in ("0.5", "0.50", ".5"):
        assert parse_invocation(["beat_jump_size", "4", token]).command == {
            "type": "beat_jump_size",
            "deck": 4,
            "beats": 0.5,
        }
    assert parse_invocation(["beat_jump", "1", "0.5"]).command["beats"] == 0.5
    assert parse_invocation(["beat_jump", "1", "-0.5"]).command["beats"] == -0.5


@pytest.mark.parametrize("token", ["0", "-0", "0.25", "1.5", "-0.5", "-4", "3", "128", "1/2", "nan"])
def test_beat_jump_size_refuses_zero_and_unsupported_fractions(token: str) -> None:
    """[if] beat_jump_size accepts zero, a negative, an unsupported fraction or an off-list size [then] the bus gets a bad size, [else stop]."""
    with pytest.raises(InvocationError, match=r"beats must be"):
        parse_invocation(["beat_jump_size", "1", token])


@pytest.mark.parametrize("token", ["0", "0.25", "1.5", "-2.5"])
def test_beat_jump_refuses_zero_and_fractions_other_than_half(token: str) -> None:
    """[if] beat_jump accepts zero or a fraction other than +-0.5 [then] the deck jumps off the grid, [else stop]."""
    with pytest.raises(InvocationError, match=r"beats must be"):
        parse_invocation(["beat_jump", "1", token])
