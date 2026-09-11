"""Dispatcher-tick health must count a successful tick, not any tick-end.

Issue #1800: `health PASS dispatcher ticked in last 70 min` followed
`tickgate-dispatcher.ts` / any `---tick-end` line, so a tick that ended
`reason=unclassified-error exit=1` still read healthy. Evidence, Thu 10 Sep
2026: every dispatcher tick from 16:31Z to 18:42Z failed that way (Claude
weekly limit) while the line stayed PASS.

[if] a failed tick, or no log at all, reports PASS [then] fail, [else stop].

Regression lines:
  - [if] only failed ticks in the 70 min window PASS [then] fail, [else stop].
  - [if] one `reason=ok` tick-end in the window does not PASS [then] fail,
    [else stop].
  - [if] a missing dispatcher.log reports FAIL (or PASS) [then] fail, [else
    stop]. No log is UNMEASURABLE, never a verdict (issue #1673).
  - [if] a fallback attempt with `exit_code=0` does not PASS [then] fail,
    [else stop]. Tick-end `reason=session-limit` is not success; the grok
    fallback's `exit_code=0` is.
"""

from __future__ import annotations

import platform
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.scripts.test_ops_fleet_kpi import (
    REPO,
    _copy_fixture,
    _env,
    _health,
    _home,
    _run,
)

pytestmark = [
    pytest.mark.requirement("OPS-16"),
    pytest.mark.skipif(
        platform.system() != "Linux",
        reason="ops/fleet/kpi.sh is Linux-only: GNU date -d/stat -c, /proc, systemd --user, tmux",
    ),
    pytest.mark.skipif(
        shutil.which("jq") is None,
        reason="ops/fleet/kpi.sh parses the gh JSON fixtures with jq",
    ),
]

LABEL = "dispatcher ticked in last 70 min"
EXCERPTS = REPO / "tests" / "fixtures" / "fleet-kpi" / "dispatcher-issue-1800"

# Frozen "now" values that place the copied dispatcher.log excerpts inside or
# outside the 70 min window. The excerpt lines themselves are verbatim copies
# from the live log cited in issue #1800; their stamps are not rewritten.
NOW_FAILED_WINDOW = "2026-09-10T17:40:00Z"  # 70 min window starts 16:30Z
NOW_OK_WINDOW = "2026-09-10T16:00:00Z"  # 70 min window starts 14:50Z
NOW_FALLBACK_WINDOW = "2026-09-11T15:26:00Z"  # 70 min window starts 14:16Z


def _unix(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())


def _verdict(
    tmp_path: Path,
    log_name: str | None,
    now_iso: str,
    *,
    tickgate_fresh: bool,
) -> str:
    """Run kpi.sh against a copied dispatcher.log excerpt.

    tickgate-dispatcher.ts is the pre-#1800 probe input. Tests set it
    independently of the log so a pass cannot be the old timestamp file
    pretending the dispatcher is healthy.
    """
    fixture = _copy_fixture(tmp_path)
    dest = fixture / "jobs" / "logs" / "dispatcher.log"
    if log_name is None:
        dest.unlink(missing_ok=True)
    else:
        dest.write_text((EXCERPTS / log_name).read_text())
    now = _unix(now_iso)
    tickgate = fixture / "jobs" / "state" / "tickgate-dispatcher.ts"
    tickgate.write_text(f"{now - 60 if tickgate_fresh else now - 9999}\n")
    env = _env(fixture, _home(tmp_path, token_profile=True))
    env["KPI_NOW_UNIX"] = str(now)
    out = _run(env).stdout
    verdicts = _health(out)
    assert LABEL in verdicts, out
    return verdicts[LABEL]


def test_fixture_excerpts_are_real_dispatcher_log_lines():
    """The committed excerpts must stay verbatim copies, not paraphrases."""
    failed = (EXCERPTS / "failed-only.log").read_text().splitlines()
    assert any(
        line
        == (
            "---tick-end 2026-09-10T16:31:22Z model=opus effort=high "
            "reason=unclassified-error exit=1---"
        )
        for line in failed
    )
    assert all("reason=ok" not in line for line in failed)
    assert all("exit_code=0" not in line for line in failed)

    ok_lines = (EXCERPTS / "one-ok.log").read_text().splitlines()
    assert "---tick-end 2026-09-10T15:24:50Z model=opus effort=high reason=ok exit=0---" in ok_lines

    fallback = (EXCERPTS / "fallback-ok.log").read_text().splitlines()
    assert any(
        line.startswith("attempt_end=2026-09-11T15:07:01Z") and "exit_code=0" in line
        for line in fallback
    )
    assert (
        "---tick-end 2026-09-11T15:07:01Z model=opus effort=high "
        "reason=session-limit exit=1---" in fallback
    )


def test_only_failed_ticks_in_the_window_fail(tmp_path):
    """[if] failed tick-end lines still read healthy [then] fail, [else stop].

    tickgate-dispatcher.ts is fresh, which is how the live probe stayed PASS
    all afternoon on Thu 10 Sep 2026 while every tick ended unclassified-error.
    """
    assert _verdict(tmp_path, "failed-only.log", NOW_FAILED_WINDOW, tickgate_fresh=True) == "FAIL"


def test_one_ok_tick_in_the_window_passes(tmp_path):
    """[if] a reason=ok tick-end in the window does not PASS [then] fail, [else stop].

    tickgate is stale so a pass cannot come from the old timestamp file.
    """
    assert _verdict(tmp_path, "one-ok.log", NOW_OK_WINDOW, tickgate_fresh=False) == "PASS"


def test_missing_dispatcher_log_is_unmeasurable_not_fail(tmp_path):
    """[if] no dispatcher.log renders FAIL or PASS [then] fail, [else stop].

    Missing input is UNMEASURABLE, never a verdict (issue #1673). A fresh
    tickgate must not stand in for a log the probe cannot read.
    """
    assert _verdict(tmp_path, None, NOW_FAILED_WINDOW, tickgate_fresh=True) == "UNMEASURABLE"


def test_fallback_attempt_exit_code_0_passes_even_if_tick_end_is_not_ok(tmp_path):
    """[if] a grok fallback with exit_code=0 still FAILs [then] fail, [else stop].

    The matching tick-end is `reason=session-limit exit=1`. That is not
    success; the fallback attempt_end with exit_code=0 is.
    """
    assert (
        _verdict(tmp_path, "fallback-ok.log", NOW_FALLBACK_WINDOW, tickgate_fresh=False) == "PASS"
    )
