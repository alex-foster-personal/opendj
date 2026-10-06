"""Merge-count window, per-threshold markers, skip comments, P0/P1 (DEVOPS-02/03).

One test per [if] line on DEVOPS-02 and DEVOPS-03. The GitHub CLI is a
FakeGh that returns real-shaped JSON; decide() is never allowed to call
git, so a shallow runner checkout cannot change the count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.gh_version_guard import GhVersionError
from scripts.periodic_window import (
    BOT_LOGIN,
    Gh,
    GhError,
    WindowDecision,
    cadence_alarm,
    decide_window,
    render_skip_comment,
    select_state_marker,
    write_github_output,
)

REPO = "private_owner/music-dj-tools"
LEDGER = "1492"
HEAD = "b" * 40
BASE_50 = "a" * 40
BASE_100 = "c" * 40
NOW = datetime(2026, 9, 12, 5, 40, tzinfo=UTC)
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "periodic-checks.yml"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bot(body: str, created_at: str = "2026-09-01T00:00:00Z") -> dict[str, Any]:
    return {"user": {"login": BOT_LOGIN}, "body": body, "created_at": created_at}


def _marker(sha: str, threshold: int = 50) -> str:
    return f"<!-- periodic-checks state sha={sha} threshold={threshold} -->"


def _row_marker(row: int, sha: str, threshold: int = 100) -> str:
    return f"<!-- periodic-checks row={row} state sha={sha} threshold={threshold} -->"


@dataclass
class FakeGh:
    """Seam for `gh api`. Records paths; never shells out, never calls git."""

    main_sha: str = HEAD
    main_date: str = "2026-09-12T05:00:00Z"
    comments: list[dict[str, Any]] = field(default_factory=list)
    compare_commits: dict[str, int] = field(default_factory=dict)
    compare_errors: dict[str, GhError] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def api_json(self, path: str) -> Any:
        self.calls.append(path)
        if path == f"repos/{REPO}/commits/main":
            return {
                "sha": self.main_sha,
                "commit": {"committer": {"date": self.main_date}},
            }
        if "/compare/" in path:
            assert path.startswith(f"repos/{REPO}/compare/"), path
            assert path.endswith("...main"), (
                "DEVOPS-02: compare the recorded SHA against main, not a resolved head "
                f"SHA (path={path})"
            )
            base = path.split("/compare/", 1)[1].removesuffix("...main")
            if base in self.compare_errors:
                raise self.compare_errors[base]
            if base not in self.compare_commits:
                raise GhError(f"no compare fixture for {base}", status=404)
            return {"total_commits": self.compare_commits[base]}
        raise AssertionError(f"unexpected gh api path: {path}")

    def api_json_paginated(self, path: str) -> list[Any]:
        self.calls.append(path)
        if path == f"repos/{REPO}/issues/{LEDGER}/comments":
            return list(self.comments)
        raise AssertionError(f"unexpected gh api path: {path}")


def _decide(gh: FakeGh, **kwargs: Any) -> WindowDecision:
    kwargs.setdefault("repo", REPO)
    kwargs.setdefault("ledger_issue", LEDGER)
    kwargs.setdefault("threshold", 50)
    kwargs.setdefault("force", False)
    kwargs.setdefault("clock_days", 7)
    kwargs.setdefault("now", NOW)
    return decide_window(gh=gh, **kwargs)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(_workflow_text())["jobs"]


def _window_script() -> str:
    job = _jobs()["window"]
    return "\n".join(str(s.get("run", "")) for s in job.get("steps") or [])


def _report_script() -> str:
    job = _jobs()["report"]
    return "\n".join(str(s.get("run", "")) for s in job.get("steps") or [])


# ---------------------------------------------------------------------------
# DEVOPS-02 [if] lines
# ---------------------------------------------------------------------------


@pytest.mark.requirement("DEVOPS-02")
def test_no_previous_state_marker_reports_bootstrap_and_runs() -> None:
    """[if] no previous state marker exists [then] report bootstrap and run.

    [if] no previous state marker exists [then] it bootstraps and runs, [else stop].
    """
    gh = FakeGh(comments=[])
    decision = _decide(gh)
    assert decision.run is True
    assert decision.record_sha is True
    assert decision.count == -1
    assert decision.base == ""
    assert "bootstrap" in decision.reason
    assert not any("/compare/" in c for c in gh.calls), (
        "a missing marker must not invent a compare base"
    )


@pytest.mark.requirement("DEVOPS-02")
def test_a_failed_window_still_records_its_state_sha() -> None:
    """[if] a window fails [then] its state SHA is still recorded.

    Recording is the report job posting the marker. always() keeps that job
    alive when a row is red; run==true is the window that actually ran.

    [if] a window fails [then] the report job still records the state SHA via always(), [else stop].
    """
    report = _jobs()["report"]
    condition = str(report.get("if", ""))
    assert "always()" in condition
    assert "needs.window.outputs.run == 'true'" in condition
    script = _report_script()
    assert "<!-- periodic-checks state sha=${HEAD} threshold=${THRESHOLD} -->" in script
    skip = _jobs()["skip-report"]
    skip_script = "\n".join(str(s.get("run", "")) for s in skip.get("steps") or [])
    assert "<!-- periodic-checks state sha=" not in skip_script


@pytest.mark.requirement("DEVOPS-02")
def test_shallow_checkout_does_not_affect_the_count() -> None:
    """[if] the runner checkout is shallow [then] the count comes from the API.

    [if] the runner checkout is shallow [then] the merge count comes from the gh api, [else stop].
    """
    gh = FakeGh(
        comments=[_bot(_marker(BASE_50), "2026-09-10T05:40:00Z")],
        compare_commits={BASE_50: 12},
    )
    decision = _decide(gh)
    assert decision.count == 12
    assert decision.run is False
    compare_calls = [c for c in gh.calls if "/compare/" in c]
    assert compare_calls == [f"repos/{REPO}/compare/{BASE_50}...main"]
    script = _window_script()
    assert "git rev-list" not in script
    assert "python3 -m scripts.periodic_window decide" in script or (
        "python -m scripts.periodic_window decide" in script
    )


@pytest.mark.requirement("DEVOPS-02")
def test_elapsed_time_arm_fires_when_count_stays_under_threshold() -> None:
    """[if] cadence is merges:N OR a clock period [then] elapsed time still fires.

    [if] 8 days elapsed with the count under threshold [then] the elapsed arm fires, [else stop].
    """
    eight_days_ago = _iso(NOW - timedelta(days=8))
    gh = FakeGh(
        comments=[_bot(_marker(BASE_50), eight_days_ago)],
        compare_commits={BASE_50: 3},
    )
    decision = _decide(gh, threshold=50, clock_days=7)
    assert decision.run is True
    assert decision.record_sha is True
    assert decision.count == 3
    assert decision.elapsed_days >= 7
    assert "elapsed" in decision.reason


@pytest.mark.requirement("DEVOPS-02")
def test_different_thresholds_use_separate_state_markers() -> None:
    """[if] two rows carry DIFFERENT thresholds [then] each has its own marker.

    [if] two rows carry different thresholds [then] each keeps its own state marker, [else stop].
    """
    comments = [
        _bot(_marker(BASE_50, 50), "2026-09-10T00:00:00Z"),
        _bot(_row_marker(10, BASE_100, 100), "2026-09-11T00:00:00Z"),
    ]
    default = select_state_marker(comments, row=None)
    row10 = select_state_marker(comments, row=10)
    assert default is not None and default.sha == BASE_50
    assert row10 is not None and row10.sha == BASE_100
    script = _window_script()
    assert "row=10" in script
    assert "WP_THRESHOLD=100" in script or "threshold=100" in script


# ---------------------------------------------------------------------------
# DEVOPS-02 supporting behaviour
# ---------------------------------------------------------------------------


def test_an_unresolvable_compare_base_bootstraps() -> None:
    gh = FakeGh(
        comments=[_bot(_marker(BASE_50), "2026-09-10T05:40:00Z")],
        compare_errors={BASE_50: GhError("Not Found", status=404)},
    )
    decision = _decide(gh)
    assert decision.run is True
    assert decision.count == -1
    assert "bootstrap" in decision.reason


def test_a_non_404_compare_error_does_not_silent_bootstrap() -> None:
    gh = FakeGh(
        comments=[_bot(_marker(BASE_50), "2026-09-10T05:40:00Z")],
        compare_errors={BASE_50: GhError("server exploded", status=500)},
    )
    with pytest.raises(GhError) as exc:
        _decide(gh)
    assert exc.value.status == 500


def test_force_runs_even_when_under_threshold() -> None:
    gh = FakeGh(
        comments=[_bot(_marker(BASE_50), "2026-09-11T05:40:00Z")],
        compare_commits={BASE_50: 2},
    )
    decision = _decide(gh, force=True)
    assert decision.run is True
    assert "forced" in decision.reason


def test_row_10_elapsed_arm_is_disabled() -> None:
    eight_days_ago = _iso(NOW - timedelta(days=8))
    gh = FakeGh(
        comments=[_bot(_row_marker(10, BASE_100, 100), eight_days_ago)],
        compare_commits={BASE_100: 3},
    )
    decision = _decide(gh, threshold=100, clock_days=0, row=10)
    assert decision.run is False
    assert decision.record_sha is False
    assert decision.count == 3


def test_count_at_threshold_runs() -> None:
    gh = FakeGh(
        comments=[_bot(_marker(BASE_50), "2026-09-11T05:40:00Z")],
        compare_commits={BASE_50: 50},
    )
    decision = _decide(gh, threshold=50)
    assert decision.run is True
    assert decision.count == 50


def test_a_forged_marker_is_ignored_and_the_bot_marker_wins() -> None:
    comments = [
        _bot(_marker(BASE_50), "2026-09-01T00:00:00Z"),
        {
            "user": {"login": "some-rando"},
            "body": _marker("f" * 40),
            "created_at": "2026-09-02T00:00:00Z",
        },
    ]
    selected = select_state_marker(comments, row=None)
    assert selected is not None
    assert selected.sha == BASE_50


def test_a_body_with_two_markers_is_not_trusted() -> None:
    body = _marker(BASE_50) + "\nquoted: " + _marker("f" * 40)
    selected = select_state_marker([_bot(body)], row=None)
    assert selected is None


def test_write_github_output_uses_row10_keys_for_windows_parity(tmp_path: Path) -> None:
    path = tmp_path / "out"
    decision = WindowDecision(
        run=True,
        count=100,
        base=BASE_100,
        base_at="2026-09-01T00:00:00Z",
        head=HEAD,
        head_at="2026-09-12T05:00:00Z",
        threshold=100,
        elapsed_days=-1,
        reason="bootstrap",
        record_sha=True,
    )
    write_github_output(path, decision, kind="windows_parity")
    text = path.read_text(encoding="utf-8")
    assert "run_windows_parity=true" in text
    assert "windows_parity_count=100" in text
    assert f"windows_parity_base={BASE_100}" in text
    assert "windows_parity_reason=bootstrap" in text
    assert "\nrun=" not in f"\n{text}"


# ---------------------------------------------------------------------------
# DEVOPS-03 [if] lines
# ---------------------------------------------------------------------------


@pytest.mark.requirement("DEVOPS-03")
def test_ledger_comment_names_a_failed_row() -> None:
    """[if] a periodic row fails [then] the ledger comment names that row as failed.

    [if] the full-suite row fails [then] the ledger comment marks row 1 as failed, [else stop].
    """
    script = _report_script()
    assert 'failure) echo "FAIL"' in script or "failure) echo 'FAIL'" in script
    assert '$(mark "$R_FULL")' in script
    assert "| 1 |" in script


@pytest.mark.requirement("DEVOPS-03")
def test_skip_under_threshold_reports_skip_and_count() -> None:
    """[if] the merge count was under threshold [then] report the skip and the count.

    [if] the merge count stays under threshold [then] the skip comment reports SKIP, [else stop].
    """
    gh = FakeGh(
        comments=[_bot(_marker(BASE_50), "2026-09-11T05:40:00Z")],
        compare_commits={BASE_50: 7},
    )
    decision = _decide(gh, threshold=50)
    assert decision.run is False
    assert decision.record_sha is False
    assert decision.count == 7
    comment = render_skip_comment(
        window_desc=f"{BASE_50[:9]}..{HEAD[:9]}",
        count=7,
        threshold=50,
        reason=decision.reason,
        run_url="https://example/run",
    )
    assert "SKIP" in comment
    assert "7" in comment
    assert "threshold 50" in comment
    assert "<!-- periodic-checks state sha=" not in comment

    skip = _jobs()["skip-report"]
    condition = str(skip.get("if", ""))
    assert "always()" in condition
    assert "needs.window.outputs.run != 'true'" in condition
    skip_script = "\n".join(str(s.get("run", "")) for s in skip.get("steps") or [])
    assert "SKIP" in skip_script
    assert "merges in window" in skip_script
    assert "<!-- periodic-checks state sha=" not in skip_script

    wp_skip = _jobs()["windows-parity-skip-report"]
    wp_if = str(wp_skip.get("if", ""))
    assert "run_windows_parity" in wp_if
    assert "!=" in wp_if


@pytest.mark.requirement("DEVOPS-03")
def test_cadence_with_zero_runs_is_p0() -> None:
    """[if] a cadence with zero runs at all [then] it is a P0.

    [if] a cadence has zero runs or only a stale one [then] cadence_alarm reports P0, [else stop].
    """
    assert cadence_alarm([], cadence_seconds=86400, now=NOW).startswith("P0:")
    stale = [
        {
            "conclusion": "success",
            "createdAt": _iso(NOW - timedelta(hours=36)),
            "url": "https://example/1",
        }
    ]
    assert cadence_alarm(stale, cadence_seconds=86400, now=NOW).startswith("P0:")
    script = _report_script()
    alarm_at = script.find("window_desc=")
    assert alarm_at != -1
    after = script[alarm_at:]
    assert "periodic_window alarm" in after or "cadence_alarm" in after or "P0:" in after


@pytest.mark.requirement("DEVOPS-03")
def test_scheduled_workflow_red_more_than_one_cadence_is_p1() -> None:
    """[if] a scheduled workflow has been red for more than one cadence [then] P1.

    Newest completed must still be red. An older failure behind a newer pass
    is the display case, not a P1.

    [if] the newest run is still red past one cadence [then] cadence_alarm reports P1, [else stop].
    """
    runs = [
        {
            "conclusion": "failure",
            "createdAt": _iso(NOW - timedelta(hours=30)),
            "url": "https://example/old",
        },
        {
            "conclusion": "failure",
            "createdAt": _iso(NOW - timedelta(hours=2)),
            "url": "https://example/new",
        },
    ]
    assert cadence_alarm(runs, cadence_seconds=86400, now=NOW).startswith("P1:")

    recovered = [
        {
            "conclusion": "failure",
            "createdAt": _iso(NOW - timedelta(hours=30)),
            "url": "https://example/old",
        },
        {
            "conclusion": "success",
            "createdAt": _iso(NOW - timedelta(hours=2)),
            "url": "https://example/new",
        },
    ]
    assert cadence_alarm(recovered, cadence_seconds=86400, now=NOW) == ""

    fresh_red = [
        {
            "conclusion": "failure",
            "createdAt": _iso(NOW - timedelta(hours=2)),
            "url": "https://example/new",
        }
    ]
    assert cadence_alarm(fresh_red, cadence_seconds=86400, now=NOW) == ""


# ----- Gh._load: the actual crash site (agentbox-15, job 103871571683) ----
#
# This module's OWN `Gh` class (not the `FakeGh` test seam above) is what
# shelled out to the real `gh` in production and died with `unknown flag:
# --slurp` on gh 2.62.0. scripts/gh_version_guard.py is the fix; these tests
# mutate the guard to prove `Gh._load` actually calls it, the same way
# tests/scripts/test_review_gh_version_guard.py proves it for `_gh`.


def test_gh_load_calls_the_version_guard_before_invoking_the_subprocess(monkeypatch) -> None:
    """[if] the installed gh is too old [then ⛔️] `Gh()._load` raises
    GhVersionError naming both versions and never reaches `subprocess.run` --
    mutate-the-guard: patches this module's own control flow, not `gh`'s API
    output (AGENTS.md protects the latter, not the former)."""
    import scripts.periodic_window as periodic_window_module

    def _reject_version(*_args: object, **_kwargs: object) -> None:
        raise GhVersionError("gh 2.62.0 is older than the minimum 2.64.0 (test double)")

    def _subprocess_should_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Gh._load must not shell out once the version guard has failed")

    monkeypatch.setattr(periodic_window_module, "require_gh_min_version", _reject_version)
    monkeypatch.setattr(periodic_window_module.subprocess, "run", _subprocess_should_not_run)

    with pytest.raises(GhVersionError, match="test double"):
        Gh()._load(["gh", "api", "--paginate", "--slurp", "repos/x/issues/1/comments"])
