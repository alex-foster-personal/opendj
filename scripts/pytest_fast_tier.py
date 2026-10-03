"""Pytest plugin: select a duration tier from the committed ledger, and refuse a ledger
that covers too little of the collection.

SMARTEST-CI round 6 (specs/ci-fail-fast.md). The fast tier is every test the ledger
(`.test_durations`, written by pytest-split) recorded under ``--fast-tier-max-seconds``.
Measured on main 5cca5d21f, Wed 16 Sep 2026: 11,974 of 14,154 tests are under 0.5 s and sum
to 745 s; the other 2,180 sum to 4,582 s. So the fast tier is 85% of the tests for 14% of
the time, which is why it runs FIRST and in FULL on every pull request, regardless of the
diff, and the slow shards keep running everything as before.

A test the ledger has never seen is INCLUDED in the fast tier: a new test is usually small,
and excluding it would mean a new test never runs early. ``--timeout`` (pytest-timeout) is
the cap on the exception.

Registered by the root ``conftest.py`` (``pytest_plugins``), because the ``.venv/bin/pytest``
console script ci.yml runs does not put the checkout on ``sys.path`` and ``-p scripts.x``
cannot import there. ``-p scripts.pytest_fast_tier`` still works under ``python -m pytest``
and is what the tests use in a rootdir with no conftest. Inert unless one of its options is
given. Its ``pytest_collection_modifyitems`` runs FIRST so the coverage floor
is measured over the whole collection before pytest-split (trylast) deselects other groups,
and so pytest-split then balances only the tier's tests.

Requirements (mini-PRD)
- [if] ``--fast-tier fast`` [then] only tests recorded under the ceiling, plus tests the
  ledger has never seen, remain selected and the rest are deselected, [else stop] ✔︎ ✅ 🎯
- [if] ``--fast-tier slow`` [then] only tests recorded at or over the ceiling remain,
  [else stop] ✔︎ ✅ 🎯
- [if] ``--ledger-coverage-min 0.95`` and the ledger names fewer than 95% of the collected
  tests [then] the run is refused as a usage error naming both counts, never exit 0,
  [else stop] ✔︎ ✅ 🎯
- [if] ``--ledger-coverage-warn 0.95`` and the ledger names fewer than 95% of the collected
  tests but no fewer than ``--ledger-coverage-min`` [then] the run goes ahead and prints one
  warning line (a GitHub ``::warning`` annotation) naming both counts, [else stop] ✔︎ ✅ 🎯
- [if] the ledger file is missing while an option is given [then] usage error naming the
  path, [else stop] ✔︎ ✅ 🎯
- [if] no option is given [then] nothing is deselected and the exit code is pytest's own,
  [else stop] ✔︎ ✅ 🎯
- [if] a tier selects zero tests [then] pytest reports "no tests ran" (exit 5); pair with
  ``scripts.pytest_tier_floor --tier-min-selected`` to make a thin selection fail loud,
  [else stop] ✔︎ ✅ 🎯
- [if] a test is named in the always-fast list (``ci/fast-tier-always.txt``: a node id, or
  a file path for every test in it) [then] it is in the fast tier whatever the ledger
  recorded and out of the slow tier, [else stop] ✔︎ ✅ 🎯
- [if] an always-fast entry matches no collected test while a tier is selected [then] usage
  error naming the entry, so the list cannot rot, [else stop] ✔︎ ✅ 🎯
- [if] a test carries ``@pytest.mark.slow`` and is not on the always-fast list [then] it is
  out of the fast tier and in the slow tier whatever the ledger recorded, unseen included,
  [else stop] ✔︎ ✅ 🎯

The ``slow`` marker rule exists because an unseen test is included in the fast tier by
design, and a test that never ran to completion is never recorded: on PR #3732 (Mon 21 Sep
2026) the playlist-switch Playwright bench (tests/perf/test_library_playlist_switch_bench.py)
became runnable for the first time, entered leg 2 as an unseen test, and ate the whole 480 s
leg budget (exit 124). A bench author knows it is slow before any ledger does; the marker
says so where the test lives, and the slow shards still run it.

The always-fast list exists because the round 7 miss audit (specs/ci-fail-fast.md, Wed 16
Sep 2026) found the fast tier caught 9 of 20 pull-request-caused failures, and 8 of the 11
misses were the same two slow requirement-marker tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_NAME: str = "fast_tier"
LINE_PREFIX: str = "[fast-tier]"
DEFAULT_LEDGER: str = ".test_durations"
DEFAULT_MAX_SECONDS: float = 0.5
DEFAULT_ALWAYS: str = "ci/fast-tier-always.txt"
SLOW_MARKER: str = "slow"
TIERS: tuple[str, ...] = ("fast", "slow", "all")
#: Ledger rows that are padding, not tests (see the collect-floor notes in ci.yml).
PAD_MARKER: str = "_ci_dur_pad"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("fast-tier", "duration tiers from the pytest-split ledger")
    group.addoption(
        "--fast-tier",
        choices=TIERS,
        default=None,
        help="select only this duration tier: fast (under the ceiling, plus unseen tests), "
        "slow (at or over it), or all (no deselection, measurement only)",
    )
    group.addoption(
        "--fast-tier-max-seconds",
        type=float,
        default=DEFAULT_MAX_SECONDS,
        help="the ceiling between fast and slow, in recorded seconds "
        f"(default {DEFAULT_MAX_SECONDS})",
    )
    group.addoption(
        "--fast-tier-durations",
        default=DEFAULT_LEDGER,
        help=f"the pytest-split durations ledger to read (default {DEFAULT_LEDGER})",
    )
    group.addoption(
        "--fast-tier-always",
        default=None,
        help="tests that are fast-tier whatever the ledger says: one node id or test file "
        f"path per line, # comments (default {DEFAULT_ALWAYS} when it exists)",
    )
    group.addoption(
        "--ledger-coverage-min",
        type=float,
        default=None,
        help="refuse the run when the ledger names fewer than this fraction of the collected "
        "tests (0.95 means 95%%); guards against shards balancing by count",
    )
    group.addoption(
        "--ledger-coverage-warn",
        type=float,
        default=None,
        help="warn, without refusing, when the ledger names fewer than this fraction of the "
        "collected tests; pairs with a lower --ledger-coverage-min",
    )


def pytest_configure(config: pytest.Config) -> None:
    no_tier = config.getoption("--fast-tier") is None
    inert = (
        no_tier
        and config.getoption("--ledger-coverage-min") is None
        and config.getoption("--ledger-coverage-warn") is None
    )
    if inert:
        return
    config.pluginmanager.register(FastTier.from_config(config), PLUGIN_NAME)


def _load_ledger(path: Path) -> dict[str, float]:
    if not path.is_file():
        raise pytest.UsageError(f"{LINE_PREFIX} durations ledger not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {node: float(seconds) for node, seconds in raw.items() if PAD_MARKER not in node}


def _load_always(path: Path, *, explicit: bool) -> frozenset[str]:
    """Node ids and test file paths forced into the fast tier. The default file may be
    absent (empty list); a path given on the command line may not."""
    if not path.is_file():
        if explicit:
            raise pytest.UsageError(f"{LINE_PREFIX} always-fast list not found: {path}")
        return frozenset()
    entries = (
        line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()
    )
    return frozenset(entry for entry in entries if entry)


def _always_matches(entry: str, nodeid: str) -> bool:
    return nodeid == entry or nodeid.startswith(entry + "::")


def _marked_slow(item: pytest.Item) -> bool:
    """``@pytest.mark.slow`` (registered in pyproject) on the test or any enclosing
    node: the author's own statement that this test never belongs in the fast tier."""
    return item.get_closest_marker(SLOW_MARKER) is not None


class FastTier:
    """Deselects by recorded duration and measures how much of the collection the ledger names."""

    def __init__(
        self,
        tier: str | None,
        max_seconds: float,
        ledger: dict[str, float],
        coverage_min: float | None,
        always: frozenset[str] = frozenset(),
        coverage_warn: float | None = None,
    ) -> None:
        self.tier = tier
        self.max_seconds = max_seconds
        self.ledger = ledger
        self.coverage_min = coverage_min
        self.coverage_warn = coverage_warn
        self.always = always
        self.summary: str = ""

    @classmethod
    def from_config(cls, config: pytest.Config) -> FastTier:
        ledger_path = Path(config.getoption("--fast-tier-durations"))
        if not ledger_path.is_absolute():
            ledger_path = Path(config.rootpath) / ledger_path
        always_option = config.getoption("--fast-tier-always")
        always_path = Path(always_option or DEFAULT_ALWAYS)
        if not always_path.is_absolute():
            always_path = Path(config.rootpath) / always_path
        return cls(
            tier=config.getoption("--fast-tier"),
            max_seconds=config.getoption("--fast-tier-max-seconds"),
            ledger=_load_ledger(ledger_path),
            coverage_min=config.getoption("--ledger-coverage-min"),
            always=_load_always(always_path, explicit=always_option is not None),
            coverage_warn=config.getoption("--ledger-coverage-warn"),
        )

    def _forced(self, nodeid: str) -> bool:
        return any(_always_matches(entry, nodeid) for entry in self.always)

    def _belongs(self, item: pytest.Item) -> bool:
        nodeid = item.nodeid
        if self.tier in ("fast", "slow") and self._forced(nodeid):
            return self.tier == "fast"
        if self.tier in ("fast", "slow") and _marked_slow(item):
            return self.tier == "slow"
        recorded = self.ledger.get(nodeid)
        if self.tier == "fast":
            return recorded is None or recorded < self.max_seconds
        if self.tier == "slow":
            return recorded is not None and recorded >= self.max_seconds
        return True  # "all": measurement only

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        collected = len(items)
        known = sum(1 for item in items if item.nodeid in self.ledger)
        coverage = known / collected if collected else 0.0
        if self.coverage_min is not None and coverage < self.coverage_min:
            raise pytest.UsageError(
                f"{LINE_PREFIX} ledger names {known} of {collected} collected tests "
                f"({coverage:.1%}), under the {self.coverage_min:.0%} floor. pytest-split "
                "balances unseen tests by a flat average, so shards are balanced by count, "
                "not time. Check that durations-ledger.yml is publishing and this run's scope job "
                "resolved it (a change set that edits `.test_durations` keeps its own file)."
            )
        if self.coverage_warn is not None and coverage < self.coverage_warn:
            # Stderr, not the terminal reporter: a GitHub annotation must start its own line,
            # and this hook runs before the reporter prints the collection summary.
            print(
                f"::warning title=fast-tier ledger is going stale::{LINE_PREFIX} ledger names "
                f"{known} of {collected} collected tests ({coverage:.1%}), under the "
                f"{self.coverage_warn:.0%} warning line; the run goes ahead. Check that "
                "durations-ledger.yml is publishing and this run's scope job resolved it.",
                file=sys.stderr,
            )
        if self.tier in ("fast", "slow"):
            stale = sorted(
                entry
                for entry in self.always
                if not any(_always_matches(entry, item.nodeid) for item in items)
            )
            if stale:
                raise pytest.UsageError(
                    f"{LINE_PREFIX} always-fast entries match no collected test: {stale}. "
                    "A renamed or deleted test leaves a stale entry; fix the list."
                )
        keep = [item for item in items if self._belongs(item)]
        deselected = [item for item in items if not self._belongs(item)]
        unseen = sum(1 for item in keep if item.nodeid not in self.ledger)
        forced = sum(1 for item in keep if self._forced(item.nodeid))
        marked = sum(1 for item in items if _marked_slow(item) and not self._forced(item.nodeid))
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = keep
        self.summary = (
            f"{LINE_PREFIX} tier={self.tier or 'none'} ceiling={self.max_seconds}s "
            f"selected={len(keep)} of {collected} (unseen included={unseen}) "
            f"ledger coverage={known}/{collected}={coverage:.1%} always-fast={forced} "
            f"marked-slow={marked}"
        )

    def pytest_report_collectionfinish(self) -> list[str]:
        return [self.summary] if self.summary else []
