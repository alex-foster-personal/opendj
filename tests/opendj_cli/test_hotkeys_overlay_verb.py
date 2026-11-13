"""#12272: ``opendj hotkeys_overlay`` opens or closes the hotkeys overlay and confirms against the mirror.

The page side (``apps/webui/frontend/tests/unit/hotkeys-overlay-command.test.mjs``)
proves the command drives the same overlay state the ``?`` key toggles and that
the UI mirror publishes ``hotkeys_overlay_open``; this side proves the CLI (and
MCP, which reaches the same bus through the generic ``command`` tool) builds the
typed command and reads the outcome back from that field.

Regression lines:
  - if ``opendj hotkeys_overlay true`` does not build the typed bus command then broken
  - if the verb confirms without the mirror reporting the overlay state then broken
  - if a non-boolean open reaches the bus then broken
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import VERBS
from apps.opendj_cli.confirm import checks_for, verdict
from apps.opendj_cli.verbs import InvocationError, parse_invocation


@pytest.mark.requirement("KEYB-14297")
@pytest.mark.parametrize(("token", "open_"), [("true", True), ("false", False)])
def test_hotkeys_overlay_builds_the_typed_bus_command(token: str, open_: bool) -> None:
    """[if] ``opendj hotkeys_overlay <bool>`` builds anything but the typed command [then] agents lose parity, [else stop]."""
    assert parse_invocation(["hotkeys_overlay", token]).command == {"type": "hotkeys_overlay", "open": open_}


@pytest.mark.requirement("KEYB-14297")
def test_hotkeys_overlay_confirms_against_the_mirror_open_field() -> None:
    """[if] the verb confirms without the mirror's hotkeys_overlay_open matching [then] state is unread, [else stop]."""
    command = parse_invocation(["hotkeys_overlay", "true"]).command
    checks = checks_for(VERBS["hotkeys_overlay"], command)
    assert [check.path for check in checks] == [("hotkeys_overlay_open",)]
    assert verdict(checks, {"hotkeys_overlay_open": True})[0] == "confirmed"
    assert verdict(checks, {"hotkeys_overlay_open": False})[0] == "unconfirmed"
    assert verdict(checks, {})[0] == "unconfirmed"


@pytest.mark.requirement("KEYB-14297")
def test_hotkeys_overlay_refuses_a_non_boolean_open() -> None:
    """[if] ``opendj hotkeys_overlay maybe`` parses [then] an untyped value reaches the bus, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["hotkeys_overlay", "maybe"])
