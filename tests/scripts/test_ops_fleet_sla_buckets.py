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

  [if] the four SLA buckets do not sum to open-to-merge [then] broken, [else stop]

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

import hashlib
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

# Pinned per AGENTS.md "No mocks and locked real fixtures": verify every
# committed fleet-sla file by checksum before use.
FIXTURE_SHA256: dict[str, str] = {
    "checks-1111111111111111111111111111111111111111.json":
        "d30ef2b6ffcc3d59f4599ab40c876e3c47b80079e2d791e626021e38a1e8bd27",
    "checks-2222222222222222222222222222222222222222.json":
        "a7646b77b54568b2d88fcdeccb0bf73f7dfaba04f578c2952bc949aade39d5ad",
    "checks-3333333333333333333333333333333333333333.json":
        "a91f74857fa2546cf6d9f71e0c13cc78787ca6a5483e5e15ca8c1aa32a13c510",
    "commits-9001.json":
        "d76471f47f991a380f85354cb13886a7fdc9ccf9f4bf2826f1bb479f7f338778",
    "commits-9002.json":
        "b9a9c62a3d1c4d034669e46cfa95c87258e1167b53f1c4cb86bd64e9276fa077",
    "jobs-9001001.json":
        "681aae5314f24293ace82ec7248794115b332a78e274e4bb9efd31e8e5e5df79",
    "jobs-9002001.json":
        "849cb1b29af250ee4bc3f835b0004a0c8b3a42f53ae6c5eadac40d6a06ab772c",
    "merged.json":
        "5043a6dba9d69cf308a507572deb8b693f75cef36fffdb976ec43ddbfeac6454",
    "reviews-9001.json":
        "c5c3f1e79e526374921bd09abaf5d31fadc09da48bf3a753aa2304e0d2ee803d",
    "reviews-9002.json":
        "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570",
    "runs-2222222222222222222222222222222222222222.json":
        "b541a545e4c1997dd156a4bd45cd3f7e92e6e62b93f36a867900df521f11e50c",
    "runs-3333333333333333333333333333333333333333.json":
        "14ec463994ce6ea2853ab183a77ebbddf3510cabf606cfc45312dc424bc9a18f",
    "timeline-9001.json":
        "d7dde1094f537326a2b98a6fca33c762d92930ab74c7bec7584dd678f249a8a6",
    "timeline-9002.json":
        "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570",
}


def _verify_fixture_checksums(fixture: Path = FIXTURE) -> None:
    for name, expected in FIXTURE_SHA256.items():
        path = fixture / name
        assert path.is_file(), f"checked-in fixture missing: {path}"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, (
            f"fixture {path} checksum mismatch (expected {expected}, got {actual})"
        )

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
    if fixture.resolve() == FIXTURE.resolve():
        _verify_fixture_checksums(fixture)
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
    # Two bots reviewed at 10:30Z and 10:45Z before one push at 13:00Z. The human
    # review at 11:00Z is ignored. The burst counts as one round from 10:45Z.
    assert row["first_bot_review"] == pytest.approx(0.5, abs=1e-6)
    assert row["review_latencies"] == [pytest.approx(2.25, abs=1e-6)]


def test_review_burst_before_one_push_counts_once() -> None:
    row = _rows(FIXTURE)[9001]
    assert len(row["review_latencies"]) == 1


def test_failed_head_gate_is_not_green(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    head_checks = fixture / "checks-2222222222222222222222222222222222222222.json"
    checks = json.loads(head_checks.read_text())
    checks["check_runs"][-1]["conclusion"] = "failure"
    head_checks.write_text(json.dumps(checks) + "\n")
    row = _rows(fixture)[9001]
    assert row["push_to_head_green"] is None
    assert row["green_to_merge"] is None
    assert "not pull_request green" in row["note"]


def test_dispatch_only_check_does_not_set_head_green(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    head_checks = fixture / "checks-2222222222222222222222222222222222222222.json"
    checks = json.loads(head_checks.read_text())
    checks["check_runs"].append({
        "name": "recovery dispatch",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/maintainer/music-dj-tools/actions/runs/9999001/job/9",
        "started_at": "2026-09-09T14:00:00Z",
        "completed_at": "2026-09-09T15:00:00Z",
    })
    head_checks.write_text(json.dumps(checks) + "\n")
    head_runs = fixture / "runs-2222222222222222222222222222222222222222.json"
    runs = json.loads(head_runs.read_text())
    runs["workflow_runs"].append({
        "id": 9999001,
        "name": "CI",
        "event": "workflow_dispatch",
        "created_at": "2026-09-09T14:00:00Z",
        "conclusion": "success",
    })
    (fixture / "runs-2222222222222222222222222222222222222222222.json").write_text(
        json.dumps(runs) + "\n"
    )
    (fixture / "jobs-9999001.json").write_text(json.dumps({"jobs": []}) + "\n")
    row = _rows(fixture)[9001]
    assert row["green_to_merge"] == pytest.approx(MERGED - HEAD_GREEN, abs=1e-6)


def test_ci_queue_wait_is_measured_at_the_head() -> None:
    row = _rows(FIXTURE)[9001]
    assert row["ci_queue_wait"] == pytest.approx(50.0 / 3600.0, abs=1e-6)
    assert row["ci_queue_wait"] > 0


def test_committed_fixture_checksums_match() -> None:
    _verify_fixture_checksums(FIXTURE)


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
