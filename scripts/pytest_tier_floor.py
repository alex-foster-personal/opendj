"""Pytest plugin: fail a test-tier run that measured nothing.

A run whose every selected test SKIPS exits 0, and so does a run whose
filter selected far fewer tests than it should. Both read as green while
measuring nothing: the "board with zero failing checks because it has zero
checks" case in ``.claude/rules/verification.md``. This plugin turns both
into failures, for the runs that opt in (``just cloudsync-fast`` and
``just cloudsync-slow``).

Registered by the root ``conftest.py`` (``pytest_plugins``); ``-p scripts.pytest_tier_floor``
under ``python -m pytest`` also works and dedupes against that registration.
It is inert unless one of its options is given.

"Executed" means the test body ran: a call-phase report that is not a skip.
A skip raised in a fixture (setup) or in the body (call) does not count, and
neither does an xfail.

Requirements (mini-PRD)
- [if] every selected test skips [then] exit nonzero and print "executed 0",
  [else stop] ✔︎ ✅ 🎯
- [if] a required marker's tests all skip while other tests run [then] exit
  nonzero naming that marker, [else stop] ✔︎ ✅ 🎯
- [if] fewer tests are selected than ``--tier-min-selected`` [then] exit
  nonzero naming both numbers, [else stop] ✔︎ ✅ 🎯
- [if] a required marker is not registered [then] refuse the run as a usage
  error, because a typo would otherwise read as "tier executed 0", [else stop]
  ✔︎ ✅ 🎯
- [if] no option is given, or every floor is met [then] the exit code is
  pytest's own, [else stop] ✔︎ ✅ 🎯
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

#: The tally that counts every selected test, whatever its markers.
ALL_TESTS: str = "all"
PLUGIN_NAME: str = "tier_floor"
LINE_PREFIX: str = "[tier-floor]"


@dataclass
class TierTally:
    selected: int = 0
    executed: int = 0


@dataclass(eq=False)  # identity hash: pytest keeps registered plugins in sets
class TierFloor:
    """Counts selected and executed tests per tier, and judges the run."""

    min_selected: int | None
    required_markers: tuple[str, ...]
    tallies: dict[str, TierTally] = field(default_factory=dict)
    tiers_by_nodeid: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tallies = {name: TierTally() for name in (ALL_TESTS, *self.required_markers)}

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        for item in session.items:
            tiers = (
                ALL_TESTS,
                *(
                    marker
                    for marker in self.required_markers
                    if item.get_closest_marker(marker) is not None
                ),
            )
            self.tiers_by_nodeid[item.nodeid] = tiers
            for tier in tiers:
                self.tallies[tier].selected += 1

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when != "call" or report.skipped:
            return
        for tier in self.tiers_by_nodeid.get(report.nodeid, ()):
            self.tallies[tier].executed += 1

    def violations(self) -> list[str]:
        found: list[str] = []
        total = self.tallies[ALL_TESTS]
        if total.executed == 0:
            found.append(
                f"executed 0 of {total.selected} selected: every test skipped "
                f"or none was selected, so this run measured nothing"
            )
        if self.min_selected is not None and total.selected < self.min_selected:
            found.append(
                f"selected {total.selected}, below the floor of "
                f"{self.min_selected}: a test module stopped collecting or the "
                f"selection filter is wrong"
            )
        for marker in self.required_markers:
            tally = self.tallies[marker]
            if tally.executed == 0:
                found.append(f"tier {marker!r} executed 0 of {tally.selected} selected")
        return found

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        write = _line_writer(session.config)
        for name, tally in self.tallies.items():
            write(f"{LINE_PREFIX} {name}: executed {tally.executed} of {tally.selected} selected")
        violations = self.violations()
        for violation in violations:
            write(f"{LINE_PREFIX} FAIL: {violation}")
        if violations and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


# ----- helpers -----------------------------------------------------------


def _line_writer(config: pytest.Config) -> Callable[[str], None]:
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        return reporter.write_line  # type: ignore[no-any-return]
    return lambda line: print(line, file=sys.stderr)


def _registered_markers(config: pytest.Config) -> set[str]:
    return {line.split(":", 1)[0].split("(", 1)[0].strip() for line in config.getini("markers")}


# ----- hooks -------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("tier-floor", "fail a tier run that measured nothing")
    group.addoption(
        "--tier-min-selected",
        type=int,
        default=None,
        metavar="N",
        help="fail when fewer than N tests survive selection (a collect floor)",
    )
    group.addoption(
        "--tier-require-executed",
        action="append",
        default=[],
        metavar="MARKER",
        help="fail when no selected test carrying MARKER executed (repeatable)",
    )


def pytest_configure(config: pytest.Config) -> None:
    min_selected: int | None = config.getoption("--tier-min-selected")
    required = tuple(config.getoption("--tier-require-executed"))
    if min_selected is None and not required:
        return
    unknown = sorted(set(required) - _registered_markers(config))
    if unknown:
        raise pytest.UsageError(
            f"--tier-require-executed names unregistered marker(s) {unknown}; "
            f"a typo here would read as a tier that executed 0"
        )
    config.pluginmanager.register(
        TierFloor(min_selected=min_selected, required_markers=required),
        PLUGIN_NAME,
    )
