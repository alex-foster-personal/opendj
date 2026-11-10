"""CUEOUT-10753: CLI output_mode accepts every mode the HTTP route accepts.

[if] output_mode split_cable is rejected [then] agents cannot reach split cue, [else stop].
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import OUTPUT_MODE_VALUES
from apps.opendj_cli.verbs import InvocationError, parse_invocation

pytestmark = pytest.mark.requirement("CUEOUT-10753")


@pytest.mark.parametrize("mode", list(OUTPUT_MODE_VALUES))
def test_output_mode_accepts_every_catalog_value(mode: str) -> None:
    """[if] output_mode <mode> builds no command [then] the catalog is unreachable, [else stop]."""
    assert parse_invocation(["output_mode", mode]).command == {
        "type": "output_mode",
        "mode": mode,
    }


def test_output_mode_rejects_an_unknown_mode() -> None:
    """[if] output_mode nope parses [then] the enum is not enforced, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["output_mode", "nope"])
