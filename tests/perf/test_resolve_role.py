"""Role resolution must refuse to guess between two running Open DJ shells.

`find_shell(rows, None)` breaks ties by lowest pid (scripts/diagnostics/
probe_process_family.py), and every per-role pid COUNT downstream always
reads as 1 regardless of which shell it came from, so nothing else catches a
profiler silently attaching to the wrong build. `_reject_ambiguous_shell` is
the guard that must fire before role resolution ever runs.
"""

from __future__ import annotations

import pytest

from scripts.diagnostics.probe_process_family import ProcessRow
from scripts.perf.resolve_role import RoleUnresolved, _reject_ambiguous_shell

SHELL_A = ProcessRow(
    pid=100, ppid=1, pgid=100, command="/Applications/Open DJ.app/Contents/MacOS/opendj-desktop"
)
SHELL_B = ProcessRow(
    pid=200, ppid=1, pgid=200, command="/Applications/Open DJ.app/Contents/MacOS/opendj-desktop"
)
NOT_A_SHELL = ProcessRow(pid=50, ppid=1, pgid=50, command="/usr/libexec/some-other-daemon")
ENGINE_OF_SHELL_A = ProcessRow(
    pid=101,
    ppid=100,
    pgid=100,
    command=(
        "/Applications/Open DJ.app/Contents/Resources/payload/runtime/bin/python3 "
        "-m apps.engine_core serve"
    ),
)


def test_two_shells_with_no_pin_is_rejected() -> None:
    """If broken: role resolution silently profiles whichever pid is lowest."""
    with pytest.raises(RoleUnresolved) as excinfo:
        _reject_ambiguous_shell([SHELL_A, SHELL_B, NOT_A_SHELL], None)
    assert "2 Open DJ desktop shells" in str(excinfo.value)
    assert "100" in str(excinfo.value)
    assert "200" in str(excinfo.value)


def test_two_shells_with_a_pin_is_allowed_through() -> None:
    """If broken: passing --shell-pid is still rejected as ambiguous."""
    _reject_ambiguous_shell([SHELL_A, SHELL_B], 100)  # must not raise


def test_one_shell_with_no_pin_is_allowed_through() -> None:
    """If broken: the common single-build case starts failing too."""
    _reject_ambiguous_shell([SHELL_A, NOT_A_SHELL], None)  # must not raise


def test_zero_shells_with_no_pin_is_allowed_through() -> None:
    """If broken: 'app not running' stops reaching its own, later, error path."""
    _reject_ambiguous_shell([NOT_A_SHELL], None)  # must not raise; find_shell reports absence


def test_the_engine_it_spawned_is_not_counted_as_a_second_shell() -> None:
    """If broken: every single-build run is rejected as ambiguous, because the
    packaged engine's own command path (`.../Open DJ.app/Contents/Resources/
    payload/runtime/bin/python3 -m apps.engine_core serve`) is launched from
    inside the same bundle the shell is, so a bundle-root marker matches both
    (PR #705 review, resolve_role.py:76, fresh evidence after this guard's own
    introduction in 5fd736cd)."""
    _reject_ambiguous_shell([SHELL_A, ENGINE_OF_SHELL_A], None)  # must not raise
