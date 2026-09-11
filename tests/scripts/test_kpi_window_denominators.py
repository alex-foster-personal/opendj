"""Regression test for the KPI window/all-time denominator split in ops/fleet/kpi.sh.

The defect this pins: done/blocked were ALL-TIME counts printed inside a block
headed "KPI window: last 6h". Measured Sat 5 Sep 2026 on the live nucbox copy,
done read 39 and blocked 76 across 118 report files while the same 6h window held
5 and 16 - a reader takes that as an 8x overstatement of throughput.

The test slices the counting logic out of the shipped script and runs it, rather
than restating it, so an edit to kpi.sh that reintroduces the all-time counts
fails here instead of drifting away from a copy.

  [if] the window counts equal the all-time counts on a fixture that deliberately
       backdates most reports outside the window [then broken]
  [if] an archived done whose live report still exists counts as erased [then broken]
  [if] an empty reports dir makes the window count anything but 0 [then broken]
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
KPI_SH = REPO_ROOT / "ops" / "fleet" / "kpi.sh"

# kpi.sh is Linux-only by design (GNU date, systemd, /proc) and its window helper
# passes `-newermt "@<epoch>"`, which GNU find parses and BSD find does not. On
# macOS with /usr/bin/find that produces "Can't parse date/time" on stderr, an
# empty result, and a test failure that says nothing about the code under test.
# Skip with the reason stated rather than fail, and rather than quietly rewriting
# the helper to something portable that the shipped script does not use - the
# script has to keep working on nucbox, which is where it actually runs.
_EPOCH_PROBE = subprocess.run(
    ["bash", "-c", 'find . -maxdepth 0 -newermt "@0" >/dev/null 2>&1'],
    capture_output=True,
)
pytestmark = pytest.mark.skipif(
    _EPOCH_PROBE.returncode != 0,
    reason="find(1) here cannot parse -newermt @<epoch> (BSD find); kpi.sh targets GNU find",
)

# The slice of kpi.sh under test: from the window helper through the erased-done
# scan. Anchored on source text so a rename or removal fails loudly here.
SLICE_START = "_win_reports()"
SLICE_END = "| sort -u | wc -l)"


def _counting_block() -> str:
    """Return the verdict-counting lines verbatim from the shipped script."""
    lines = KPI_SH.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(SLICE_START)]
    assert len(starts) == 1, f"expected one {SLICE_START} in {KPI_SH}, found {len(starts)}"
    start = starts[0]
    ends = [i for i, ln in enumerate(lines[start:], start) if ln.rstrip().endswith(SLICE_END)]
    assert ends, f"no line ending {SLICE_END!r} after {SLICE_START} in {KPI_SH}"
    return "\n".join(lines[start : ends[0] + 1])


def _build_fixture(root: Path) -> None:
    """24 report files: 4 written now (3 done, 1 blocked), 20 done backdated 48h.

    Plus two archived reports - one whose issue has no live report (a genuinely
    erased done) and one whose issue does (must not be counted).
    """
    reports = root / "reports"
    (reports / "archive").mkdir(parents=True)

    for n in (101, 102, 103):
        (reports / f"issue-{n}.md").write_text("RESULT: done\n", encoding="utf-8")
    (reports / "issue-104.md").write_text("RESULT: blocked\n", encoding="utf-8")

    stale = time.time() - 48 * 3600
    for n in range(200, 220):
        f = reports / f"issue-{n}.md"
        f.write_text("RESULT: done\n", encoding="utf-8")
        os.utime(f, (stale, stale))

    (reports / "archive" / "issue-900--attempt1.md").write_text("RESULT: done\n", encoding="utf-8")
    (reports / "archive" / "issue-101--attempt1.md").write_text("RESULT: done\n", encoding="utf-8")


def _run_block(jobs: Path, hours: int = 6) -> dict[str, str]:
    script = f'set -u\nH={hours}\nJOBS="{jobs}"\nnow=$(date -u +%s)\n{_counting_block()}\n' + (
        'printf "%s %s %s %s %s\\n" '
        '"$done_n" "$blocked_n" "$done_all" "$reports_total" "$done_erased"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60, check=True
    ).stdout.split()
    keys = ("done_n", "blocked_n", "done_all", "reports_total", "done_erased")
    return dict(zip(keys, out, strict=True))


def test_window_counts_are_scoped_to_the_window(tmp_path: Path) -> None:
    _build_fixture(tmp_path)
    got = _run_block(tmp_path)

    assert got["done_n"] == "3", f"window done should exclude the 48h-old reports, got {got}"
    assert got["blocked_n"] == "1", f"window blocked wrong, got {got}"
    assert got["done_all"] == "23", f"all-time done should include them, got {got}"
    assert got["reports_total"] == "24", f"denominator wrong, got {got}"

    # The control: if these ever match, the fixture stopped being able to see the
    # defect and every assertion above is passing for free.
    assert got["done_n"] != got["done_all"], (
        "window and all-time agree, so this fixture cannot detect the defect it pins"
    )


def test_only_archived_dones_without_a_live_report_count_as_erased(tmp_path: Path) -> None:
    _build_fixture(tmp_path)
    got = _run_block(tmp_path)
    # issue-900 is erased; issue-101 is archived but still has a live report.
    assert got["done_erased"] == "1", f"erased count wrong, got {got}"


def test_empty_reports_dir_counts_zero_and_does_not_hang(tmp_path: Path) -> None:
    (tmp_path / "reports" / "archive").mkdir(parents=True)
    got = _run_block(tmp_path)
    assert got["done_n"] == "0", got
    assert got["blocked_n"] == "0", got
    assert got["done_erased"] == "0", got


@pytest.mark.parametrize("hours", [1, 24])
def test_window_tracks_the_hours_argument(tmp_path: Path, hours: int) -> None:
    _build_fixture(tmp_path)
    got = _run_block(tmp_path, hours=hours)
    # 48h-old reports stay outside both a 1h and a 24h window, so the window count
    # is the 3 fresh dones either way; only a window wider than 48h would change it.
    assert got["done_n"] == "3", f"H={hours} gave {got}"


# --- blocked-quality: a `blocked` verdict is not evidence anything is blocked ----

BQ_START = "stale_blocked=0"
BQ_END = "done"


def _blocked_quality_block() -> str:
    """Return the blocked-quality counting loop verbatim from the shipped script."""
    lines = KPI_SH.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == BQ_START]
    assert len(starts) == 1, f"expected one {BQ_START!r} in {KPI_SH}, found {len(starts)}"
    start = starts[0]
    ends = [i for i, ln in enumerate(lines[start:], start) if ln.strip() == BQ_END]
    assert ends, f"no loop terminator after {BQ_START} in {KPI_SH}"
    return "\n".join(lines[start : ends[0] + 1])


def _run_blocked_quality(jobs: Path, merged: str, hours: int = 24) -> tuple[str, str]:
    script = (
        f'set -u\nH={hours}\nJOBS="{jobs}"\nmerged_nums="{merged}"\n'
        'now=$(date -u +%s)\n'
        # mirror the shipped _win_reports, which is -newermt against $now so the
        # KPI_NOW_UNIX seam freezes the window; -mmin would silently ignore it.
        '_win_reports() { find "$JOBS/reports" -maxdepth 1 -name "issue-*.md" '
        '-newermt "@$(( now - H * 3600 ))" 2>/dev/null; }\n'
        f"{_blocked_quality_block()}\n"
        'printf "%s %s\\n" "$stale_blocked" "$named_pr"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60, check=True
    ).stdout.split()
    return out[0], out[1]


def _blocked(reports: Path, issue: int, pr: int | None) -> None:
    body = f"PR: #{pr}\n" if pr else ""
    (reports / f"issue-{issue}.md").write_text(body + "RESULT: blocked\n", encoding="utf-8")


def test_blocked_reports_with_no_pr_line_leave_the_denominator_alone(tmp_path: Path) -> None:
    """The bug this pins: counting PR-less reports as 'not stale' deflates the rate.

    That is the same denominator error the whole block exists to correct, and it
    shipped once before this test existed.
    """
    reports = tmp_path / "reports"
    (reports / "archive").mkdir(parents=True)
    _blocked(reports, 1, 900)  # merged  -> numerator and denominator
    _blocked(reports, 2, 901)  # merged  -> numerator and denominator
    _blocked(reports, 3, 902)  # open    -> denominator only
    _blocked(reports, 4, None)  # no PR  -> NEITHER
    _blocked(reports, 5, None)  # no PR  -> NEITHER

    stale, named = _run_blocked_quality(tmp_path, merged=" 900 901 ")
    assert (stale, named) == ("2", "3"), (
        f"got {stale} of {named}; PR-less reports must not enter the denominator"
    )


def test_substring_pr_numbers_do_not_false_match(tmp_path: Path) -> None:
    """PR #82 must not match against merged #1182 - the lookup is space-delimited."""
    reports = tmp_path / "reports"
    (reports / "archive").mkdir(parents=True)
    _blocked(reports, 1, 82)
    stale, named = _run_blocked_quality(tmp_path, merged=" 1182 820 ")
    assert (stale, named) == ("0", "1"), f"got {stale} of {named}; #82 false-matched"
