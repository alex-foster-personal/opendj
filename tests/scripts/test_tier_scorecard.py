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


def _run(jobs: Path, prs: Path | None, expect_ok: bool = True, **env_extra):
    env = {"TIER_JOBS_DIR": str(jobs), "PATH": "/usr/bin:/bin", **env_extra}
    if prs is not None:
        env["TIER_GH_PRS"] = str(prs)
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env,
                       timeout=120, check=False)
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
