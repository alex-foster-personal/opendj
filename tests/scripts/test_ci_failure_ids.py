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

from scripts.ci_failure_ids import (
    KNOWN_FLAKES,
    JobVerdict,
    classify_job,
    failed_identities,
    failure_beyond_tests,
    unowned_exit_steps,
    unpartitionable_exit,
)

pytestmark = pytest.mark.requirement("OPS-16")

TS = "2026-09-16T09:12:01.1234567Z "
SEP = "\u203a"  # Playwright's list reporter separator
_EXIT = "Process completed with exit code 1."


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
    got = classify_job(
        "pytest fast lane (shard 1 of 5)",
        ONE,
        frozenset(),
        frozenset(),
        beyond_tests=False,
    )
    assert got.verdict is JobVerdict.GENUINE
    assert got.residual == ONE


def test_a_failure_on_main_is_known_red():
    got = classify_job("pytest fast lane (shard 1 of 5)", ONE, ONE, frozenset(), beyond_tests=False)
    assert got.verdict is JobVerdict.KNOWN_RED


def test_a_known_flake_is_known_red():
    flake = frozenset({"not ok - prefs-golden-blob.test.mjs bundles"})
    assert (
        classify_job("frontend unit", flake, frozenset(), frozenset(), beyond_tests=False).verdict
        is JobVerdict.KNOWN_RED
    )


def test_a_shard_with_no_identity_is_infra():
    got = classify_job(
        "pytest fast lane (shard 2 of 5)",
        frozenset(),
        frozenset(),
        frozenset(),
        beyond_tests=False,
    )
    assert got.verdict is JobVerdict.INFRA


def test_a_ratchet_breach_is_debt():
    got = classify_job(
        "quality ratchet (lint debt)",
        frozenset(),
        frozenset(),
        frozenset(),
        beyond_tests=False,
    )
    assert got.verdict is JobVerdict.RATCHET_DEBT


def test_a_ratchet_job_failing_a_test_main_passes_is_genuine():
    """Keyed on the job name alone, a real regression landing in a ratchet job reads as debt,
    the watcher ends KNOWN_RED_ONLY, and the agent merges past it."""
    got = classify_job(
        "quality ratchet (lint debt)",
        frozenset({"FAILED tests/test_new.py::test_regression"}),
        frozenset(),
        frozenset(),
        beyond_tests=False,
    )
    assert got.verdict is JobVerdict.GENUINE


def test_a_ratchet_job_failing_only_what_main_fails_is_still_debt():
    """The control: the fix must not turn every ratchet breach into a blocker."""
    shared = "FAILED tests/test_old.py::test_known"
    got = classify_job(
        "quality ratchet (lint debt)",
        frozenset({shared}),
        frozenset({shared}),
        frozenset(),
        beyond_tests=False,
    )
    assert got.verdict is JobVerdict.KNOWN_RED


def test_a_zero_identity_job_also_red_on_main_is_unmeasured_not_known_red():
    """Sol's P1 on #3293, and this test asserted the opposite before it. Every name in
    `main_red_job_names` is one whose main log was READ and named failing TESTS; this job
    named none, so the two failures are established to be DIFFERENT causes that happen to
    share a job name. Read as main's red, a build or budget regression merges."""
    name = "reqs-check + native wheel + contract drift"
    got = classify_job(name, frozenset(), frozenset(), frozenset({name}), beyond_tests=False)
    assert got.verdict is JobVerdict.BASELINE_MISMATCH


def test_a_zero_identity_job_not_red_on_main_is_loud():
    name = "reqs-check + native wheel + contract drift"
    got = classify_job(
        name, frozenset(), frozenset(), frozenset({"some other job"}), beyond_tests=False
    )
    assert got.verdict is JobVerdict.GENUINE


def test_a_job_whose_baseline_log_was_unreadable_is_not_main_red():
    """The exploit path: main failed this job too, but its log was never read, so nothing
    establishes the two failures are the same. Classified as main's own red it exits mergeable
    off a baseline that was never measured."""
    name = "frontend unit + check + build"
    got = classify_job(
        name, frozenset(), frozenset(), frozenset(), frozenset({name}), beyond_tests=False
    )
    assert got.verdict is JobVerdict.BASELINE_UNREADABLE


def test_a_measured_main_red_job_name_is_still_distinguished_from_an_unreadable_one():
    """The control against the overshoot: collapsing both into one verdict would lose WHY a
    job is unmeasured, which is the difference between main's log saying something else
    failed and main's log not being readable at all. Both are unmeasured; neither merges."""
    name = "frontend unit + check + build"
    measured = classify_job(
        name, frozenset(), frozenset(), frozenset({name}), frozenset(), beyond_tests=False
    )
    unreadable = classify_job(
        name, frozenset(), frozenset(), frozenset(), frozenset({name}), beyond_tests=False
    )
    assert measured.verdict is JobVerdict.BASELINE_MISMATCH
    assert unreadable.verdict is JobVerdict.BASELINE_UNREADABLE


# ----- a job that also failed for something no test explains -----


def test_known_identities_plus_a_cap_kill_is_not_known_red():
    """Sol's P1 on #3293. A shard can fail tests main already fails AND be killed for a
    budget or a timeout. Read as KNOWN_RED the agent merges on the strength of the tests,
    and the kill is never looked at."""
    shared = "FAILED tests/test_old.py::test_known"
    got = classify_job(
        "pytest fast lane (shard 1 of 5)",
        frozenset({shared}),
        frozenset({shared}),
        frozenset(),
        beyond_tests=True,
    )
    assert got.verdict is JobVerdict.UNEXPLAINED


def test_known_identities_alone_are_still_known_red():
    """The control, in the direction the fix could overshoot: without it, every known-red
    job becomes unmeasured and the watcher stops being able to say a pull request is clean."""
    shared = "FAILED tests/test_old.py::test_known"
    got = classify_job(
        "pytest fast lane (shard 1 of 5)",
        frozenset({shared}),
        frozenset({shared}),
        frozenset(),
        beyond_tests=False,
    )
    assert got.verdict is JobVerdict.KNOWN_RED


def test_a_zero_identity_job_keeps_its_own_verdict():
    """The other control: the new rule is scoped to jobs that DID report identities. A cap
    kill with no identity at all is still INFRA, which the unmeasured bucket already held."""
    got = classify_job(
        "pytest fast lane (shard 2 of 5)",
        frozenset(),
        frozenset(),
        frozenset(),
        beyond_tests=True,
    )
    assert got.verdict is JobVerdict.INFRA


@pytest.mark.parametrize(
    "line",
    [
        "##[error]The operation was canceled.",
        "##[error]The job running on runner agentbox-4 has exceeded the maximum execution time",
        "##[error]Process completed with exit code 137.",
        "##[error]Process completed with exit code 2.",
        "cp: error writing '/x': No space left on device",
    ],
)
def test_a_runner_failure_no_test_explains_is_found(line):
    assert failure_beyond_tests(f"some output\n{line}\nmore output") == [line]


@pytest.mark.parametrize(
    "line",
    [
        "##[error]Process completed with exit code 1.",
        "FAILED tests/test_a.py::test_b - AssertionError",
        "=========== 3 failed, 2118 passed, 40 skipped in 277.98s ============",
        "ERROR tests/test_c.py",
    ],
)
def test_an_ordinary_test_failure_is_not_read_as_something_beyond_it(line):
    """Exit code 1 is what pytest, vitest and playwright return when tests fail, so it is the
    one code the identities can account for. Matching it would make every red job unmeasured."""
    assert failure_beyond_tests(f"some output\n{line}\nmore output") == []


def test_a_new_failure_plus_a_cap_kill_is_still_genuine():
    """Ordering. UNEXPLAINED is the verdict for a job whose failures are ALL already main's;
    a job that broke something new is GENUINE whatever else went wrong beside it, or the
    loudest verdict this tool has gets downgraded to unmeasured by an unrelated kill."""
    got = classify_job(
        "pytest fast lane (shard 1 of 5)",
        frozenset({"FAILED tests/test_new.py::test_this_pr_broke"}),
        frozenset({"FAILED tests/test_old.py::test_known"}),
        frozenset(),
        beyond_tests=True,
    )
    assert got.verdict is JobVerdict.GENUINE


def _step(command: str, *output: str) -> str:
    echo = [f"{TS}##[group]Run {command}", f"{TS}##[endgroup]"]
    return "\n".join([*echo, *(TS + line for line in output)])


def test_a_combined_job_whose_exit_came_from_a_non_test_step_is_beyond_the_tests():
    """Sol's P1 on #3293. Exit code 1 is the one code a test failure CAN explain, so it is
    deliberately not matched as beyond-the-tests -- which let a job that ran tests in one step
    and a build, typecheck or ratchet in another be explained by identities from the first
    while the SECOND is what failed. The runner opens each step with its own command echo, so
    which step owns the terminating error is readable rather than guessed."""
    log = "\n".join(
        [
            _step("pnpm vitest run", "FAILED tests/test_a.py::test_b - AssertionError"),
            _step("pnpm build", "error TS2345: argument not assignable", f"##[error]{_EXIT}"),
        ]
    )
    beyond = failure_beyond_tests(log)
    assert beyond == ["exit code 1 in a step that named no failing test: pnpm build"]


def test_a_shard_whose_exit_came_from_the_test_step_is_not_beyond_the_tests():
    """The control against the overshoot, and the case this tool spends its life on: an
    ordinary red pytest shard exits 1 from the step that printed the failures. Flagging that
    would make every known-red shard unmeasured and nothing would ever merge."""
    log = _step(
        "uv run pytest tests -x",
        "FAILED tests/test_a.py::test_b - AssertionError",
        "=========== 1 failed, 2118 passed in 277.98s ============",
        f"##[error]{_EXIT}",
    )
    assert failure_beyond_tests(log) == []


def test_a_log_with_no_step_echoes_reports_nothing_rather_than_guessing():
    """A log that cannot be partitioned into steps cannot say which step owns the exit. Empty
    is UNKNOWN here, not proof the tests owned it, which is what the caller's docstring says."""
    log = f"{TS}FAILED tests/test_a.py::test_b\n{TS}##[error]{_EXIT}\n"
    assert failure_beyond_tests(log) == []


def test_a_ratchet_job_that_also_died_beyond_the_tests_is_unmeasured():
    """Sol's P1 on #3293. RATCHET_DEBT is the only mergeable zero-identity verdict, so it is
    the one that must read the beyond-the-tests evidence. A cap-killed or disk-full ratchet
    job measured no ratchet, and shipping that as debt merges a job nobody read."""
    got = classify_job(
        "quality ratchet (lint debt)", frozenset(), frozenset(), frozenset(), beyond_tests=True
    )
    assert got.verdict is JobVerdict.UNEXPLAINED


def test_a_ratchet_job_that_merely_breached_still_ships_as_debt():
    """The control: the fix must not turn every ratchet breach into a blocker, which is what
    the verdict exists to avoid under SHIP mode."""
    got = classify_job(
        "quality ratchet (lint debt)", frozenset(), frozenset(), frozenset(), beyond_tests=False
    )
    assert got.verdict is JobVerdict.RATCHET_DEBT


# ----- a known flake is a whole identity, not a substring of one -----


def test_each_known_flake_is_still_excused_where_it_really_appears():
    """The presence half: every name in the list matches in the identity shape it occurs in,
    or the tightening silently retired the fleet's flake tolerance instead of bounding it."""
    for identity in (
        "FAILED tests/ops/test_lyrics.py::test_lifespan_runs_and_joins_a_real_lyric_index_thread",
        "[chromium] › tests/setup-entry-points.spec.ts:4:1",
        "not ok - prefs-golden-blob.test.mjs > writes the blob",
    ):
        assert KNOWN_FLAKES.search(identity), identity


def test_a_test_whose_name_merely_extends_a_known_flake_is_not_excused():
    """Sol's P1 on #3293. Unanchored, a new test that extends a flake's name inherited its
    excuse and took the mergeable KNOWN_RED verdict while failing genuinely."""
    for identity in (
        "FAILED tests/ops/t.py::test_lifespan_runs_and_joins_a_real_lyric_index_thread_teardown",
        "[chromium] › tests/my_setup-entry-points.spec.ts:4:1",
        "not ok - prefs-golden-blob.test.mjsx > writes",
    ):
        assert not KNOWN_FLAKES.search(identity), identity


def test_the_dots_in_a_flake_name_are_literal():
    """`.` in an unescaped pattern matched any character, so a neighbouring spec file one
    edit away from a flake's name was excused by it."""
    assert not KNOWN_FLAKES.search("[chromium] › tests/setup-entry-pointsxspecxts:4:1")


def test_a_flaky_job_is_known_red_end_to_end():
    """The rule is tested above; this is the WIRING, which a rule test cannot reach: the
    classifier really consults it, so a job failing only on flakes still merges."""
    got = classify_job(
        "pytest fast lane (shard 2 of 5)",
        frozenset(
            {"FAILED tests/ops/test_l.py::test_lifespan_runs_and_joins_a_real_lyric_index_thread"}
        ),
        frozenset(),
        frozenset(),
        beyond_tests=False,
    )
    assert got.verdict is JobVerdict.KNOWN_RED


# ----- a step that states its own breach has explained its own exit -----


_RATCHET_LOG = (
    "##[group]Run uv run python -m scripts.quality_gate --check\n"
    "[quality] REGRESSION        ruff-debt 41 -> 44\n"
    "[quality] FAIL: 1 metric(s) got worse.\n"
    "##[error]Process completed with exit code 1\n"
)


def test_a_ratchet_breach_is_not_an_exit_nobody_owns():
    """Sol's P2 on #3293. A breach is an identity-free exit 1 BY DESIGN, and it is the one
    such exit the fleet ships as debt. Read as unowned, every ordinary ratchet log came back
    UNEXPLAINED and the documented mergeable debt path ended UNKNOWN on every real run."""
    assert unowned_exit_steps(_RATCHET_LOG) == []
    assert failure_beyond_tests(_RATCHET_LOG) == []


def test_a_ratchet_job_whose_breach_is_readable_is_debt_not_unexplained():
    got = classify_job(
        "quality ratchet (lint debt, complexity, coupling, dead code)",
        frozenset(),
        frozenset(),
        frozenset(),
        beyond_tests=bool(failure_beyond_tests(_RATCHET_LOG)),
    )
    assert got.verdict is JobVerdict.RATCHET_DEBT


def test_a_ratchet_step_that_died_without_stating_a_breach_is_still_unowned():
    """The control against the overshoot: the excuse belongs to a step that SAID why it
    failed, not to every step the ratchet job runs. A ratchet killed mid-measurement
    measured no ratchet, and must not inherit the debt path."""
    killed = (
        "##[group]Run uv run python -m scripts.quality_gate --check\n"
        "##[error]Process completed with exit code 1\n"
    )
    assert unowned_exit_steps(killed) != []


# ----- an exit nobody could attribute is unmeasured, not owned -----


_UNPARTITIONABLE = (
    "FAILED tests/a/test_x.py::test_one - AssertionError\n"
    "##[error]Process completed with exit code 1\n"
)


def test_an_exit_in_a_log_with_no_steps_is_reported_unpartitionable():
    """Sol's P1 on #3293. `unowned_exit_steps` returns empty for two opposite reasons -- no
    unowned exit, and no way to tell -- and the caller read both as "the tests own it", so a
    log full of known-red identities plus an unattributable exit merged."""
    assert unowned_exit_steps(_UNPARTITIONABLE) == []
    assert unpartitionable_exit(_UNPARTITIONABLE) is True


def test_a_log_that_partitions_is_not_reported_unpartitionable():
    """The control: the tri-state must distinguish, not just report UNKNOWN everywhere. A log
    whose exit sits in a step that named the failing test is MEASURED, and still merges."""
    owned = (
        "##[group]Run pytest\n"
        "FAILED tests/a/test_x.py::test_one - AssertionError\n"
        "##[error]Process completed with exit code 1\n"
    )
    assert unpartitionable_exit(owned) is False
    assert unowned_exit_steps(owned) == []


def test_a_log_with_no_exit_at_all_is_not_reported_unpartitionable():
    """The other control: absence of steps is only a problem when there is an exit to
    attribute. A job that failed its conclusion without an exit-1 line is not this case."""
    assert unpartitionable_exit("FAILED tests/a/test_x.py::test_one\n") is False
