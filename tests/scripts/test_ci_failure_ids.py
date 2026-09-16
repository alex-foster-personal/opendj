"""Tests for scripts/ci_failure_ids.py, the in-repo port of the fleet gate's log parser.

Parity with nucbox ~/jobs/sweep/failed-ids.sh was measured Wed 16 Sep 2026 on 17 real failed
job logs (e2e gate, pytest shards, frontend, quality ratchet, contracts): 17 identical outputs,
11 of them non-empty. These tests pin each format so a later edit cannot drift silently.

Regression lines:
  - if a pytest FAILED or ERROR line yields no identity then broken
  - if a TAP failure keeps its shifting test number then broken
  - if a Playwright flaky or progress line reads as a failure then broken
  - if a svelte-check Warn block reads as a failure then broken
  - if a failure also on main reads GENUINE then broken
  - if a failed job with zero identities reads anything but loud (GENUINE) outside the
    infra, ratchet and main-red-job rules then broken
"""

from __future__ import annotations

import pytest

from scripts.ci_failure_ids import JobVerdict, classify_job, failed_identities

pytestmark = pytest.mark.requirement("OPS-16")

TS = "2026-09-16T09:12:01.1234567Z "
SEP = "\u203a"  # Playwright's list reporter separator


def test_pytest_failed_and_error_lines_with_runner_timestamps():
    log = (
        f"{TS}FAILED tests/a/test_x.py::test_one - AssertionError\n"
        f"{TS}ERROR tests/b/test_y.py::test_two\n"
        f"{TS}PASSED tests/c/test_z.py::test_three\n"
    )
    assert failed_identities(log) == {
        "FAILED tests/a/test_x.py::test_one",
        "ERROR tests/b/test_y.py::test_two",
    }


def test_tap_failure_drops_the_test_number():
    log = f"{TS}not ok 97 - the source menu is keyboard reachable  \n{TS}ok 98 - fine\n"
    assert failed_identities(log) == {"not ok - the source menu is keyboard reachable"}


def test_spec_reporter_failure_reads_as_the_same_identity_as_tap():
    tap = failed_identities(f"{TS}not ok 3 - loads the deck\n")
    spec = failed_identities(f"{TS}✖ loads the deck (12.5ms)\n")
    assert tap == spec == {"not ok - loads the deck"}


def test_playwright_reads_only_the_failed_block():
    log = "\n".join(
        [
            f"{TS}  ✓  1 [chromium] {SEP} tests/e2e/ok.spec.ts:3:1 {SEP} passes (2.0s)",
            f"{TS}  2 failed",
            f"{TS}    [chromium] {SEP} tests/e2e/deck.spec.ts:10:5 {SEP} loads",
            f"{TS}    [webkit] {SEP} tests/e2e/fx.spec.ts:44:3 {SEP} echoes",
            f"{TS}  1 flaky",
            f"{TS}    [chromium] {SEP} tests/e2e/retry.spec.ts:7:1 {SEP} passes on retry",
        ]
    )
    assert failed_identities(log) == {
        f"[chromium] {SEP} tests/e2e/deck.spec.ts:10:5",
        f"[webkit] {SEP} tests/e2e/fx.spec.ts:44:3",
    }


def test_svelte_check_reads_error_blocks_not_warn_blocks():
    checkout = "/home/runner/_work/music-dj-tools/music-dj-tools/"
    log = "\n".join(
        [
            f"{TS}\x1b[36m{checkout}apps/webui/frontend/src/A.svelte:12:4\x1b[0m",
            f"{TS}Error: Type 'string' is not assignable",
            f"{TS}{checkout}apps/webui/frontend/src/B.svelte:3:1",
            f"{TS}Warn: unused export",
        ]
    )
    assert failed_identities(log) == {"svelte-check apps/webui/frontend/src/A.svelte:12"}


def test_a_clean_log_has_no_identities():
    assert failed_identities(f"{TS}12 passed in 3.1s\n") == frozenset()


ONE = frozenset({"FAILED tests/a/test_x.py::test_one"})


def test_a_failure_not_on_main_is_genuine():
    got = classify_job("pytest fast lane (shard 1 of 5)", ONE, frozenset(), frozenset())
    assert got.verdict is JobVerdict.GENUINE
    assert got.residual == ONE


def test_a_failure_on_main_is_known_red():
    got = classify_job("pytest fast lane (shard 1 of 5)", ONE, ONE, frozenset())
    assert got.verdict is JobVerdict.KNOWN_RED


def test_a_known_flake_is_known_red():
    flake = frozenset({"not ok - prefs-golden-blob.test.mjs bundles"})
    assert (
        classify_job("frontend unit", flake, frozenset(), frozenset()).verdict
        is JobVerdict.KNOWN_RED
    )


def test_a_shard_with_no_identity_is_infra():
    got = classify_job("pytest fast lane (shard 2 of 5)", frozenset(), frozenset(), frozenset())
    assert got.verdict is JobVerdict.INFRA


def test_a_ratchet_breach_is_debt():
    got = classify_job("quality ratchet (lint debt)", frozenset(), frozenset(), frozenset())
    assert got.verdict is JobVerdict.RATCHET_DEBT


def test_a_ratchet_job_failing_a_test_main_passes_is_genuine():
    """Keyed on the job name alone, a real regression landing in a ratchet job reads as debt,
    the watcher ends KNOWN_RED_ONLY, and the agent merges past it."""
    got = classify_job(
        "quality ratchet (lint debt)",
        frozenset({"FAILED tests/test_new.py::test_regression"}),
        frozenset(),
        frozenset(),
    )
    assert got.verdict is JobVerdict.GENUINE


def test_a_ratchet_job_failing_only_what_main_fails_is_still_debt():
    """The control: the fix must not turn every ratchet breach into a blocker."""
    shared = "FAILED tests/test_old.py::test_known"
    got = classify_job(
        "quality ratchet (lint debt)", frozenset({shared}), frozenset({shared}), frozenset()
    )
    assert got.verdict is JobVerdict.KNOWN_RED


def test_a_zero_identity_job_also_red_on_main_is_main_red_job():
    name = "reqs-check + native wheel + contract drift"
    got = classify_job(name, frozenset(), frozenset(), frozenset({name}))
    assert got.verdict is JobVerdict.MAIN_RED_JOB


def test_a_zero_identity_job_not_red_on_main_is_loud():
    name = "reqs-check + native wheel + contract drift"
    got = classify_job(name, frozenset(), frozenset(), frozenset({"some other job"}))
    assert got.verdict is JobVerdict.GENUINE
