"""Wiring tests for ops/fleet/sla_buckets.py, the open-to-merge stage split.

Hermetic by construction: the script's SLA_GH_FIXTURES_DIR seam points every
GitHub read at the committed fixture under tests/fixtures/fleet-sla/ instead of
the network, so the tests run anywhere and never touch the repo. The fixture
holds two merged PRs whose timestamps are fixed, so every bucket below is an
exact number rather than a tolerance.

The property under test is the one the whole instrument rests on: the four
buckets SUM to open-to-merge. A bucket set that merely looks plausible while
dropping the hours it cannot attribute is how a stage split reports the wrong
stage as the biggest.

Regression lines:
  - if the four buckets do not sum to open-to-merge then broken (4.5 h on the
    fully measured fixture PR, and the sum is asserted against the total, not
    against a hardcoded constant that could drift with it)
  - if a SKIPPED check-run counts as a CI result then broken: the fixture's
    first commit has a skipped check at 10:01 and an executed one at 10:20, so
    counting skipped reports a 1-minute first-result bucket for a PR whose tests
    had not started, and every path-filtered PR reads as instantly reviewed
  - if head green is the FIRST terminal check rather than the LAST then broken
    (the fixture head's quality ratchet ends at 13:05 and its E2E at 13:40; green
    is 13:40)
  - if a stage boundary outside the open-to-merge interval is not clamped then
    broken: the second fixture PR's branch was pushed an hour BEFORE the PR
    existed, so an unclamped first result is negative and the buckets stop
    summing
  - if a merge that beat the head's checks is not reported then broken: the
    second fixture PR merged at 09:30 and its only check-run ended at 10:00, and
    B4 is clamped to 0 while merged_before_green carries the truth
  - if a clamped bucket goes negative then broken (both fixture PRs assert
    every bucket >= 0, which is the reader-visible contract of the table)
  - if the review-to-next-push latency counts a review with no push after it as
    zero then broken (the fixture's second PR has no reviews at all and must
    report none, not an instant response)
  - if a non-bot review contributes to the review latency then broken (the
    fixture's second review is a human and only the bot's 10:30 review counts)
  - if ci_queue_wait is measured across commits instead of at the head then
    broken: the fixture's first commit has no workflow runs, so a cross-commit
    read subtracts a later head run from an earlier start and goes negative
  - if a missing fixture file is read as empty then broken (the run must fail
    loudly and name the file)
  - if an empty merged-PR list reports a zero denominator instead of refusing
    then broken
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("OPS-16")

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "ops" / "fleet" / "sla_buckets.py"
FIXTURE = REPO / "tests" / "fixtures" / "fleet-sla"
DAY = "2026-09-09"

# Fixture anchors in hours from midnight, so the expectations read as the
# timeline they encode. Written as clock arithmetic, never as rounded decimals:
# 0.33 is not 20 minutes and an assertion against it is wrong in the third
# decimal place for no reason.
OPENED = 10.0  # 10:00Z, PR 9001 opened
FIRST_RESULT = 10 + 20 / 60  # 10:20Z, the first EXECUTED check at its first commit
LAST_PUSH = 13.0  # 13:00Z, head pushed
HEAD_GREEN = 13 + 40 / 60  # 13:40Z, the head's slowest check ends
MERGED = 14.5  # 14:30Z


def _run(fixture: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLA_")}
    env["SLA_GH_FIXTURES_DIR"] = str(fixture)
    return subprocess.run(
        ["uv", "run", "--no-project", "--quiet", "python", str(SCRIPT), "--merged-on", DAY, *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def _rows(fixture: Path) -> dict[int, dict[str, object]]:
    result = _run(fixture, "--json")
    assert result.returncode == 0, result.stderr
    return {int(row["number"]): row for row in json.loads(result.stdout)}


def test_buckets_sum_to_open_to_merge() -> None:
    row = _rows(FIXTURE)[9001]
    assert row["open_to_merge"] == pytest.approx(4.5, abs=1e-6)
    total = sum(row[key] for key in (
        "open_to_first_result", "result_to_last_push", "push_to_head_green", "green_to_merge"))
    assert total == pytest.approx(row["open_to_merge"], abs=1e-6)


def test_each_stage_is_the_interval_it_names() -> None:
    row = _rows(FIXTURE)[9001]
    assert row["open_to_first_result"] == pytest.approx(FIRST_RESULT - OPENED, abs=1e-6)
    assert row["result_to_last_push"] == pytest.approx(LAST_PUSH - FIRST_RESULT, abs=1e-6)
    assert row["push_to_head_green"] == pytest.approx(HEAD_GREEN - LAST_PUSH, abs=1e-6)
    assert row["green_to_merge"] == pytest.approx(MERGED - HEAD_GREEN, abs=1e-6)


def test_a_skipped_check_is_not_a_ci_result() -> None:
    row = _rows(FIXTURE)[9001]
    assert row["open_to_first_result"] == pytest.approx(FIRST_RESULT - OPENED, abs=1e-6)
    assert row["open_to_first_result"] > 0.1


def test_head_green_is_the_last_terminal_check() -> None:
    row = _rows(FIXTURE)[9001]
    assert row["green_to_merge"] == pytest.approx(MERGED - HEAD_GREEN, abs=1e-6)


def test_boundaries_outside_the_interval_are_clamped_not_negative() -> None:
    row = _rows(FIXTURE)[9002]
    assert row["open_to_merge"] == pytest.approx(0.5, abs=1e-6)
    assert row["open_to_first_result"] == pytest.approx(0.5, abs=1e-6)
    assert row["merged_before_green"] is True
    assert row["green_to_merge"] == pytest.approx(0.0, abs=1e-6)


def test_no_bucket_is_ever_negative() -> None:
    for row in _rows(FIXTURE).values():
        for key in ("open_to_first_result", "result_to_last_push",
                    "push_to_head_green", "green_to_merge"):
            assert row[key] >= 0, (row["number"], key)


def test_review_latency_skips_reviews_with_no_push_after_them() -> None:
    rows = _rows(FIXTURE)
    assert rows[9002]["review_latencies"] == []
    assert rows[9002]["first_bot_review"] is None


def test_only_bot_reviews_count_toward_review_latency() -> None:
    row = _rows(FIXTURE)[9001]
    # The bot reviewed at 10:30Z and the next push was 13:00Z. The human review
    # at 11:00Z would have made this 1.0 h.
    assert row["first_bot_review"] == pytest.approx(0.5, abs=1e-6)
    assert row["review_latencies"] == [pytest.approx(2.5, abs=1e-6)]


def test_ci_queue_wait_is_measured_at_the_head() -> None:
    row = _rows(FIXTURE)[9001]
    assert row["ci_queue_wait"] == pytest.approx(50.0 / 3600.0, abs=1e-6)
    assert row["ci_queue_wait"] > 0


def test_a_missing_fixture_is_a_loud_failure(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    (fixture / "commits-9001.json").unlink()
    result = _run(fixture)
    assert result.returncode != 0
    assert "missing fixture" in result.stderr
    assert "commits-9001.json" in result.stderr


def test_an_empty_denominator_refuses(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    (fixture / "merged.json").write_text("[]\n")
    result = _run(fixture)
    assert result.returncode != 0
    assert "empty denominator" in result.stderr
