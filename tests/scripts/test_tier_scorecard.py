"""tier-scorecard.py: compare worker tiers on outcomes that actually landed.

The scorecard exists so "deepseek is proven" stays a PROCEDURE that can be
re-derived, rather than a verdict with its timestamp removed. These tests pin
the properties that make its numbers safe to quote.

[if] two tiers both worked an issue [then] that issue is in NEITHER tier's merge
  denominator [broken if a shared issue is credited to one of them].
[if] a single-tier issue produced no branch-linked PR [then] it is excluded from
  the rate and counted under noPR [broken if a research job reads as a failure].
[if] the GitHub read returns nothing [then] the run is FATAL [broken if every
  tier renders 0 merges, a false zero that looks like a measurement].
[if] a log is not issue-<digits>.log [then] it is skipped AND reported [broken if
  a mis-invocation silently folds into a real issue].
[if] no gate run ever returned RED-MINE [then] the scorecard warns the gate's
  failing direction is unproven [broken if 0 reads as a clean bill of health].
[if] any gate run DID return RED-MINE [then] that warning is ABSENT [broken if
  the warning is unconditional text rather than a real check].

Every scenario runs the REAL script against real files through its documented
env seams. No mocks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "fleet" / "tier-scorecard.py"


#---------------------------------------------------------------- fixtures
def _attempt(tier: str, start: str = "2026-09-08T10:00:00Z", sid: str = "sid-1") -> str:
    return (f"TIER={tier} ACCOUNT=acct MODEL_ARGS=--model m EFFORT=medium "
            f"AUTOCOMPACT=300000 MAX_TURNS=800 MAX_SECONDS=7200 MODE=fresh "
            f"SID={sid} ATTEMPT_START={start}")


def _pr(number: int, ref: str, state: str = "MERGED") -> dict:
    return {"number": number, "headRefName": ref, "state": state, "mergedAt": "x"}


def _write_log(jobs: Path, issue: str, body: str) -> None:
    (jobs / "logs").mkdir(parents=True, exist_ok=True)
    (jobs / "logs" / f"issue-{issue}.log").write_text(body)


def _prs(jobs: Path, entries: list[dict]) -> Path:
    p = jobs / "prs.json"
    p.write_text(json.dumps(entries))
    return p


def _run(jobs: Path, prs: Path | None, expect_ok: bool = True,
          cli_args: list[str] | None = None, **env_extra):
    env = {"TIER_JOBS_DIR": str(jobs), "PATH": "/usr/bin:/bin", **env_extra}
    if prs is not None:
        env["TIER_GH_PRS"] = str(prs)
    r = subprocess.run([sys.executable, str(SCRIPT), *(cli_args or [])],
                       capture_output=True, text=True, env=env, timeout=120, check=False)
    if expect_ok:
        assert r.returncode == 0, f"scorecard failed: {r.stdout}\n{r.stderr}"
    return r


#---------------------------------------------------------------- attribution
def test_issue_worked_by_two_tiers_is_credited_to_neither(tmp_path: Path) -> None:
    """A shared issue leaves both denominators; silently giving it to one is how a
    scorecard lies about the cheap tier that started the work."""
    jobs = tmp_path / "jobs"
    _write_log(jobs, "500", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=10\n"
                            f"{_attempt('advisor')}\nEXIT=0 ended_by=self turns=20\n")
    _write_log(jobs, "501", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=11\n")
    prs = _prs(jobs, [
        _pr(9500, "af--issue-500--shared"),
        _pr(9501, "af--issue-501--solo"),
    ])
    out = _run(jobs, prs).stdout
    ds = next(ln for ln in out.splitlines() if ln.startswith("deepseek"))
    # 2 attempts, but only #501 is single-tier, so w/PR and merged are both 1.
    assert ds.split()[1] == "2", f"expected 2 attempts, got: {ds}"
    assert ds.split()[2] == "1", f"expected 1 single-tier issue, got: {ds}"
    assert ds.split()[3] == "1" and ds.split()[4] == "1", f"expected 1/1 merged, got: {ds}"
    adv = next(ln for ln in out.splitlines() if ln.startswith("advisor"))
    assert adv.split()[2] == "0", f"advisor must claim no single-tier issue, got: {adv}"


def test_issue_with_no_branch_linked_pr_leaves_the_rate_denominator(tmp_path: Path) -> None:
    """Research and pricing jobs end with no branch by design. Counting them as
    failures understates every tier that gets them."""
    jobs = tmp_path / "jobs"
    _write_log(jobs, "600", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=45\n")
    _write_log(jobs, "601", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=45\n")
    prs = _prs(jobs, [_pr(9600, "af--issue-600--real")])
    out = _run(jobs, prs).stdout
    ds = next(ln for ln in out.splitlines() if ln.startswith("deepseek"))
    f = ds.split()
    assert f[2] == "2", f"two single-tier issues expected: {ds}"
    assert f[3] == "1", f"only the one with a PR belongs in the denominator: {ds}"
    assert f[4] == "1", f"that one merged: {ds}"
    assert f[6] == "1", f"the PR-less issue must be counted under noPR: {ds}"


#---------------------------------------------------------------- fail-fast rails
def test_empty_github_read_is_fatal_not_a_scorecard_of_zeroes(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    _write_log(jobs, "700", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\n")
    prs = _prs(jobs, [])
    r = _run(jobs, prs, expect_ok=False)
    assert r.returncode != 0, "an empty PR list must not render as 0 merges"
    assert "0 pull requests" in r.stderr


def test_missing_logs_directory_is_fatal(tmp_path: Path) -> None:
    r = _run(tmp_path / "nope", None, expect_ok=False)
    assert r.returncode != 0
    assert "no logs directory" in r.stderr


def test_malformed_log_name_is_skipped_and_reported(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    _write_log(jobs, "800", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\n")
    (jobs / "logs" / "issue-opusplan.log").write_text(f"{_attempt('opusplan')}\n")
    (jobs / "logs" / "issue-.log").write_text(f"{_attempt('sonnet')}\n")
    prs = _prs(jobs, [_pr(9800, "af--issue-800--x")])
    out = _run(jobs, prs).stdout
    assert "malformed log name(s) skipped" in out
    assert "issue-opusplan.log" in out and "issue-.log" in out
    assert not any(ln.startswith("opusplan") for ln in out.splitlines()), \
        "a mis-invocation must not create a tier row"


#---------------------------------------------------------------- the gate warning, both directions
def test_all_unknown_gates_warn_that_red_mine_never_fired(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    _write_log(jobs, "900", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\nGATE rc=2\n")
    _write_log(jobs, "901", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\nGATE rc=0\n")
    prs = _prs(jobs, [_pr(9900, "af--issue-900--x")])
    out = _run(jobs, prs).stdout
    assert "RED-MINE is 0 across all" in out


def test_a_real_red_mine_gate_silences_the_warning(tmp_path: Path) -> None:
    """The opposite direction. A warning that cannot switch off is decoration, not
    a check - and would keep firing after the gate was fixed."""
    jobs = tmp_path / "jobs"
    _write_log(jobs, "910", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\nGATE rc=1\n")
    prs = _prs(jobs, [_pr(9910, "af--issue-910--x")])
    out = _run(jobs, prs).stdout
    assert "RED-MINE is 0 across all" not in out, "warning must clear once the gate bites"


#---------------------------------------------------------------- cohort boundary
def test_since_isolates_post_swap_cohort_from_polluted_all_time_rate(tmp_path: Path) -> None:
    """The reviewer's own arithmetic (PR #1635 thread 1): an all-time read mixes an
    old cohort (7 merged of 8) with a new post-swap cohort (4 merged of 8) into a
    contaminated 11/16 that clears a 9/14 stop-rule threshold the new cohort alone
    does not. --since must isolate the 8-issue post-swap cohort at 4 merged, not
    the polluted combined rate."""
    jobs = tmp_path / "jobs"
    boundary = "2026-09-09T21:00:00Z"
    old_start = "2026-09-01T10:00:00Z"
    new_start = "2026-09-10T02:00:00Z"
    prs: list[dict] = []
    for n in range(200, 208):  # old cohort: 8 issues, 7 merged
        _write_log(jobs, str(n), f"{_attempt('deepseek', start=old_start)}\n"
                                  f"EXIT=0 ended_by=self turns=10\n")
        prs.append(_pr(9000 + n, f"af--issue-{n}--old", state="MERGED" if n < 207 else "OPEN"))
    for n in range(300, 308):  # new cohort: 8 issues, 4 merged
        _write_log(jobs, str(n), f"{_attempt('deepseek', start=new_start)}\n"
                                  f"EXIT=0 ended_by=self turns=10\n")
        prs.append(_pr(9000 + n, f"af--issue-{n}--new", state="MERGED" if n < 304 else "OPEN"))
    prs_path = _prs(jobs, prs)

    out_all = _run(jobs, prs_path).stdout
    ds_all = next(ln for ln in out_all.splitlines() if ln.startswith("deepseek"))
    assert ds_all.split()[3] == "16" and ds_all.split()[4] == "11", \
        f"unfiltered read must show the polluted 11/16: {ds_all}"

    out_cohort = _run(jobs, prs_path, cli_args=["--since", boundary]).stdout
    ds_cohort = next(ln for ln in out_cohort.splitlines() if ln.startswith("deepseek"))
    assert ds_cohort.split()[3] == "8", f"post-swap cohort must be exactly 8, not: {ds_cohort}"
    assert ds_cohort.split()[4] == "4", \
        f"post-swap cohort must show 4 merged (below the 9/14 threshold), got: {ds_cohort}"
    assert boundary in out_cohort, "the scored cohort's boundary must be stated in the output"


def test_no_since_labels_the_cohort_as_all_time(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    _write_log(jobs, "1100", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\n")
    prs = _prs(jobs, [_pr(91100, "af--issue-1100--x")])
    out = _run(jobs, prs).stdout
    assert "Cohort: all-time" in out


def test_since_excludes_an_issue_whose_earlier_tier_predates_the_boundary(tmp_path: Path) -> None:
    """Reviewer finding (PR #1635 new thread, tier-scorecard.py:297): a naive
    per-ATTEMPT --since filter drops the old attempt but keeps the issue, so an
    issue with a pre-boundary sonnet attempt and a post-boundary deepseek retry
    reads as a fresh single-tier deepseek issue and pulls its pre-existing merge
    into the new cohort. The boundary must be drawn on the issue's EARLIEST
    attempt across all tiers, so this issue is excluded from deepseek's cohort
    entirely - not credited to deepseek at all."""
    jobs = tmp_path / "jobs"
    boundary = "2026-09-09T21:00:00Z"
    pre = "2026-09-01T10:00:00Z"
    post = "2026-09-10T02:00:00Z"
    _write_log(jobs, "1200",
               f"{_attempt('sonnet', start=pre, sid='sid-a')}\nEXIT=0 ended_by=self turns=30\n"
               f"{_attempt('deepseek', start=post, sid='sid-b')}\nEXIT=0 ended_by=self turns=10\n")
    _write_log(jobs, "1201", f"{_attempt('deepseek', start=post)}\nEXIT=0 ended_by=self turns=5\n")
    prs = _prs(jobs, [
        _pr(91200, "af--issue-1200--straddles", state="MERGED"),
        _pr(91201, "af--issue-1201--clean-new", state="MERGED"),
    ])
    out = _run(jobs, prs, cli_args=["--since", boundary]).stdout
    ds = next(ln for ln in out.splitlines() if ln.startswith("deepseek"))
    f = ds.split()
    assert f[2] == "1", f"#1200 straddles the boundary and must not count as single-tier: {ds}"
    assert f[3] == "1" and f[4] == "1", \
        f"only #1201 (clean, entirely post-boundary) belongs to the cohort: {ds}"


def test_cohort_line_does_not_mislabel_a_straddling_issues_newer_attempt_as_older(
    tmp_path: Path,
) -> None:
    """Reviewer finding (P2, tier-scorecard.py:246): the excluded-attempts count
    used to be labeled unconditionally 'older', but a straddling issue's
    post-boundary retry is excluded because its ISSUE predates the boundary, not
    because that attempt itself is old. The message must not claim it is."""
    jobs = tmp_path / "jobs"
    boundary = "2026-09-09T21:00:00Z"
    pre = "2026-09-01T10:00:00Z"
    post = "2026-09-10T02:00:00Z"
    _write_log(jobs, "2100",
               f"{_attempt('sonnet', start=pre, sid='sid-a')}\nEXIT=0 ended_by=self turns=30\n"
               f"{_attempt('deepseek', start=post, sid='sid-b')}\nEXIT=0 ended_by=self turns=10\n")
    _write_log(jobs, "2101", f"{_attempt('deepseek', start=post)}\nEXIT=0 ended_by=self turns=5\n")
    prs = _prs(jobs, [
        _pr(92100, "af--issue-2100--straddles", state="MERGED"),
        _pr(92101, "af--issue-2101--clean-new", state="MERGED"),
    ])
    out = _run(jobs, prs, cli_args=["--since", boundary]).stdout
    assert "older attempt" not in out, \
        f"a post-boundary retry excluded via its issue must not be called older: {out}"


#---------------------------------------------------------------- PR read cap
def test_pr_read_at_the_cap_is_fatal_not_a_silent_truncation(tmp_path: Path) -> None:
    """Past the gh pr list read cap, older outcomes vanish into noPR for every
    tier without a word. A result sitting exactly at the cap must be treated as
    truncated and refused, not scored."""
    jobs = tmp_path / "jobs"
    _write_log(jobs, "1200", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\n")
    capped_prs = [_pr(i, f"af--issue-{9000 + i}--filler", state="OPEN") for i in range(2000)]
    prs = _prs(jobs, capped_prs)
    r = _run(jobs, prs, expect_ok=False)
    assert r.returncode != 0, "a PR list at the read cap must not render as a score"
    assert "cap" in r.stderr, f"stderr did not explain why: {r.stderr}"


def test_pr_read_under_the_cap_is_not_flagged(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    _write_log(jobs, "1300", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\n")
    under_cap_prs = [_pr(i, f"af--issue-{9000 + i}--filler", state="OPEN") for i in range(1999)]
    prs = _prs(jobs, under_cap_prs)
    _run(jobs, prs, expect_ok=True)


#---------------------------------------------------------------- terminal PR outcomes
def test_open_prs_are_excluded_from_the_rate_not_scored_as_failures(tmp_path: Path) -> None:
    """Reviewer finding (PR #1635 new thread, tier-scorecard.py:214): with_pr used
    to include issues whose PR was still OPEN, so an in-review PR was scored as a
    non-merge and could depress the rate below the round's stop-rule threshold
    before that PR ever reached an outcome. Only MERGED/CLOSED (terminal) PRs
    belong in the rate denominator; an OPEN PR is pending, not a failure."""
    jobs = tmp_path / "jobs"
    for n in (2000, 2001, 2002):
        _write_log(jobs, str(n), f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=10\n")
    prs = _prs(jobs, [
        _pr(92000, "af--issue-2000--a", state="MERGED"),
        _pr(92001, "af--issue-2001--b", state="MERGED"),
        _pr(92002, "af--issue-2002--c", state="OPEN"),
    ])
    out = _run(jobs, prs).stdout
    ds = next(ln for ln in out.splitlines() if ln.startswith("deepseek"))
    f = ds.split()
    assert f[3] == "3", f"all three issues have a PR: {ds}"
    assert f[4] == "2", f"only the two merged PRs count as merges: {ds}"
    assert "2/2*" in ds, f"rate denominator must be the 2 TERMINAL PRs, not all 3: {ds}"
    assert f[6] == "0", f"all three have a PR, so noPR must be 0: {ds}"
    assert f[7] == "1", f"the still-open PR must show under pend, not vanish: {ds}"


def test_closed_unmerged_prs_still_count_as_a_terminal_non_merge(tmp_path: Path) -> None:
    """The opposite direction of the pending fix above: a CLOSED (rejected, not
    merged) PR is a real outcome and must still count against the rate, not be
    excused into pend alongside a genuinely open one."""
    jobs = tmp_path / "jobs"
    _write_log(jobs, "2200", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=10\n")
    prs = _prs(jobs, [_pr(92200, "af--issue-2200--rejected", state="CLOSED")])
    out = _run(jobs, prs).stdout
    ds = next(ln for ln in out.splitlines() if ln.startswith("deepseek"))
    f = ds.split()
    assert f[4] == "0", f"a closed, unmerged PR must not count as a merge: {ds}"
    assert "0/1*" in ds, f"the closed PR is a terminal outcome, not pending: {ds}"
    assert f[7] == "0", f"a closed PR is not pending: {ds}"


#---------------------------------------------------------------- small-n honesty
def test_small_cohorts_are_quoted_as_fractions_not_rates(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    _write_log(jobs, "1000", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\n")
    prs = _prs(jobs, [_pr(91000, "af--issue-1000--x")])
    out = _run(jobs, prs).stdout
    ds = next(ln for ln in out.splitlines() if ln.startswith("deepseek"))
    assert "1/1*" in ds, f"a single issue must not be quoted as 100%: {ds}"


def test_branch_digits_attach_to_the_whole_number_not_a_prefix(tmp_path: Path) -> None:
    r"""A branch naming 1140 belongs to #1140 and to nothing that merely starts the
    same way. Both halves asserted in one run: the prefix issue must stay at zero
    WHILE the real one is credited, so a regex that stopped matching altogether
    could not pass this by making both sides empty.

    Note for future edits: `\d+` is greedy, so the `(?!\d)` lookahead in the
    script is defensive documentation rather than the thing keeping this green -
    removing it leaves this test passing. Do not read this test as covering it.
    """
    jobs = tmp_path / "jobs"
    _write_log(jobs, "114", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\n")
    _write_log(jobs, "1140", f"{_attempt('sonnet')}\nEXIT=0 ended_by=self turns=5\n")
    prs = _prs(jobs, [_pr(1142, "af--issue-1140-rb-parity")])
    out = _run(jobs, prs).stdout
    ds = next(ln for ln in out.splitlines() if ln.startswith("deepseek"))
    sn = next(ln for ln in out.splitlines() if ln.startswith("sonnet"))
    assert ds.split()[3] == "0", f"#114 must not claim issue-1140's branch: {ds}"
    assert sn.split()[3] == "1" and sn.split()[4] == "1", \
        f"#1140 must be credited the branch that names it: {sn}"


def test_branch_owner_only_a_slug_naming_a_second_issue_is_not_double_indexed(
    tmp_path: Path,
) -> None:
    """Reviewer finding (PR #1635 new thread, tier-scorecard.py:71): an unanchored
    search matches every 'issue-N' substring, so a branch like
    af--issue-123--follow-up-issue-456 gets indexed under BOTH issues and a PR it
    did not produce lands in #456's denominator. Only the leading af--issue-<N>
    owner segment may match."""
    jobs = tmp_path / "jobs"
    _write_log(jobs, "123", f"{_attempt('deepseek')}\nEXIT=0 ended_by=self turns=5\n")
    _write_log(jobs, "456", f"{_attempt('advisor')}\nEXIT=0 ended_by=self turns=5\n")
    prs = _prs(jobs, [_pr(90123, "af--issue-123--follow-up-issue-456", state="MERGED")])
    out = _run(jobs, prs).stdout
    ds = next(ln for ln in out.splitlines() if ln.startswith("deepseek"))
    adv = next(ln for ln in out.splitlines() if ln.startswith("advisor"))
    assert ds.split()[3] == "1" and ds.split()[4] == "1", \
        f"#123 (the owner) must be credited the PR: {ds}"
    assert adv.split()[3] == "0", \
        f"#456 (named only in the slug) must not also claim this PR: {adv}"
