"""A phantom run (GitHub reports `in_progress` forever) never wedges a bookkeeping pass.

[if] a phantom run is in flight [then] no pass waits or fails on it; a live run holds, [else stop].

Fleet-af issue #138. On Thu 1 Oct 2026 runs 36802069871 and 36803336485 read
`in_progress` for 17h+ while cancel and force-cancel both returned HTTP 409. Two passes
could be pinned by such a run:

  - the cost guard's and stable evidence's census (`scripts/ci_run_batch.py census`)
    held the mark for every watched run in flight since before the lookback, so the
    pass failed on every cadence;
  - the trunk-tip-only closed-PR sweep POSTed a cancel GitHub answers with 409, raised,
    and failed on every cadence, leaving every closed-PR run listed after it running.

Regression lines:
  - if a phantom still holds the census, or is not named in its output, then broken
  - if a recent in_progress run older than the lookback no longer holds, then broken
    (the overshoot control: an age rule that skips everything also "fixes" the wedge)
  - if a queued run of any age is called a phantom, then broken
  - if the closed-PR sweep POSTs a cancel for a phantom, or stops at one, then broken
  - if the closed-PR sweep stops cancelling a recent in_progress closed-PR run, then broken
  - if PHANTOM_AFTER_HOURS falls to or below the longest timeout chain of any workflow,
    the longest census lookback, or the 6.0 h measured run span, then broken
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Self

import pytest
import yaml

import scripts.ci_run_batch as batch
import scripts.ci_trunk_tip_only as tip
from scripts.ci_phantom_runs import PHANTOM_AFTER_HOURS, is_phantom, split_phantoms

pytestmark = pytest.mark.requirement("OPS-46")

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
NOW = datetime(2026, 10, 1, 19, 0, 0, tzinfo=UTC)
LOOKBACK = timedelta(hours=3)
WATCHED = {"CI", "E2E"}
# The longest creation-to-completion span measured on a live run, cited in
# ci-cost-guard.yml's census comment (one CI run, Mon 28 Sep 2026).
MEASURED_LONGEST_RUN_HOURS = 6.0


def _run(
    run_id: int, *, hours_old: float, status: str = "in_progress", name: str = "CI", attempt: int = 1
) -> dict[str, Any]:
    created = NOW - timedelta(hours=hours_old)
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "run_attempt": attempt,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ----- the rule -----


def test_an_old_in_progress_run_is_a_phantom() -> None:
    assert is_phantom(_run(36802069871, hours_old=17), NOW)


def test_a_recent_in_progress_run_is_not_a_phantom() -> None:
    assert not is_phantom(_run(1, hours_old=4), NOW)
    assert not is_phantom(_run(2, hours_old=PHANTOM_AFTER_HOURS), NOW), "the bound is strict"


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested"])
def test_a_queued_run_of_any_age_is_never_a_phantom(status: str) -> None:
    """A job queued on a saturated pool is bounded by no timeout (Codex P1 on #3844)."""
    assert not is_phantom(_run(3, hours_old=200, status=status), NOW)


def test_a_run_with_no_created_at_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="no created_at"):
        is_phantom({"id": 4, "status": "in_progress"}, NOW)


def test_split_keeps_order_and_loses_nothing() -> None:
    runs = [_run(1, hours_old=20), _run(2, hours_old=1), _run(3, hours_old=30)]
    live, phantom = split_phantoms(runs, NOW)
    assert [r["id"] for r in live] == [2]
    assert [r["id"] for r in phantom] == [1, 3]


# ----- the census (cost guard, stable evidence) -----


def test_census_skips_a_phantom_and_names_it() -> None:
    inflight = [_run(36802069871, hours_old=17), _run(36803336485, hours_old=17.5)]
    assert batch.runs_held_back(inflight, WATCHED, NOW, LOOKBACK) == []
    named = batch.phantoms_held_back(inflight, WATCHED, NOW, LOOKBACK)
    assert [r["id"] for r in named] == [36802069871, 36803336485]


def test_census_still_holds_a_recent_in_progress_run() -> None:
    """The overshoot control. 4 h is past the 3 h lookback and well under the phantom bound:
    the run is alive, the next pass cannot list it, so the mark must stay."""
    inflight = [_run(10, hours_old=4), _run(11, hours_old=17)]
    assert [r["id"] for r in batch.runs_held_back(inflight, WATCHED, NOW, LOOKBACK)] == [10]
    assert [r["id"] for r in batch.phantoms_held_back(inflight, WATCHED, NOW, LOOKBACK)] == [11]


def test_census_still_holds_an_old_queued_run() -> None:
    inflight = [_run(12, hours_old=30, status="queued")]
    assert [r["id"] for r in batch.runs_held_back(inflight, WATCHED, NOW, LOOKBACK)] == [12]
    assert batch.phantoms_held_back(inflight, WATCHED, NOW, LOOKBACK) == []


def test_census_phantom_rule_leaves_unwatched_and_rerun_filters_alone() -> None:
    inflight = [_run(13, hours_old=20, name="Docs"), _run(14, hours_old=20, attempt=2)]
    assert batch.runs_held_back(inflight, WATCHED, NOW, LOOKBACK) == []
    assert batch.phantoms_held_back(inflight, WATCHED, NOW, LOOKBACK) == []


def test_census_cli_reports_phantoms_by_id_and_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through `main`: held=0 is what the workflow's last step reads, the ids
    land in the log, the step output and the job summary."""
    inflight = [_run(36802069871, hours_old=17), _run(20, hours_old=1)]
    monkeypatch.setattr(batch, "fetch_inflight_runs", lambda *a, **k: inflight)
    monkeypatch.setattr(batch, "datetime", _FrozenDatetime)
    output, summary = tmp_path / "out", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_TOKEN", "unused")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    code = batch.main(["census", "--repository", "o/r", "--watched", "CI,E2E", "--lookback-hours", "3"])

    assert code == 0
    out = capsys.readouterr().out
    assert "[census] in_flight=2 held=0 phantom=1 phantom_ids=36802069871" in out
    assert "::warning::phantom-run-skipped caller=ci_run_batch census run_id=36802069871" in out
    assert "held=0\n" in output.read_text(encoding="utf-8")
    assert "phantom_ids=36802069871\n" in output.read_text(encoding="utf-8")
    assert "run_id=36802069871" in summary.read_text(encoding="utf-8")


def test_census_cli_still_holds_a_live_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inflight = [_run(21, hours_old=4)]
    monkeypatch.setattr(batch, "fetch_inflight_runs", lambda *a, **k: inflight)
    monkeypatch.setattr(batch, "datetime", _FrozenDatetime)
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_TOKEN", "unused")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    batch.main(["census", "--repository", "o/r", "--watched", "CI", "--lookback-hours", "3"])

    assert "held=1\n" in output.read_text(encoding="utf-8")
    assert "phantom=0 phantom_ids=none" in capsys.readouterr().out


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(NOW.timestamp(), tz=UTC)


# ----- the trunk-tip-only closed-PR sweep -----


def _pr_run(run_id: int, *, hours_old: float, status: str = "in_progress") -> tip.QueuedRun:
    return tip.QueuedRun(
        run_id=run_id,
        name="CI",
        event="pull_request",
        head_branch=f"af--closed-{run_id}",
        head_repo_owner="maintainer",
        head_sha="b" * 40,
        created_at=NOW - timedelta(hours=hours_old),
        status=status,
    )


def _record(seen: list[int], run_id: int) -> bool:
    seen.append(run_id)
    return True


def _recording_cancel(posts: list[int]):
    def cancel(run_id: int) -> tip.CancelOutcome:
        posts.append(run_id)
        return tip.CancelOutcome.CANCELLED

    return cancel


def test_closed_pr_sweep_skips_a_phantom_and_cancels_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The phantom comes FIRST, so a sweep that raised on it would never reach run 31."""
    posts: list[int] = []
    rechecked: list[int] = []
    monkeypatch.setattr(tip, "_cancel_run", _recording_cancel(posts))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    counts = tip.execute_closed_pr_sweep(
        (_pr_run(36803336485, hours_old=17), _pr_run(31, hours_old=1)),
        dry_run=False,
        still_closed=lambda run: _record(rechecked, run.run_id),
        now=NOW,
    )

    assert posts == [31], "no cancel is POSTed for the phantom"
    assert rechecked == [31], "the phantom costs no recheck call either"
    assert (counts.planned, counts.cancelled, counts.phantom_skipped) == (1, 1, (36803336485,))
    out = capsys.readouterr().out
    assert "::warning::phantom-run-skipped caller=closed-PR sweep run_id=36803336485" in out
    assert "run_id=36803336485" in summary.read_text(encoding="utf-8")


def test_closed_pr_sweep_still_cancels_a_recent_in_progress_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overshoot control: an in_progress run of a closed PR is the sweep's main case."""
    posts: list[int] = []
    monkeypatch.setattr(tip, "_cancel_run", _recording_cancel(posts))
    counts = tip.execute_closed_pr_sweep(
        (_pr_run(32, hours_old=5), _pr_run(33, hours_old=40, status="queued")),
        dry_run=False,
        still_closed=lambda _run: True,
        now=NOW,
    )
    assert posts == [32, 33]
    assert counts.phantom_skipped == ()


def test_the_listing_carries_status_into_the_sweep() -> None:
    """Without status on the parsed run, no listed run could ever read as a phantom."""
    run = tip._parse_queued_run(
        {
            "id": 34,
            "name": "CI",
            "event": "pull_request",
            "status": "in_progress",
            "head_branch": "af--x",
            "head_repository": {"owner": {"login": "maintainer"}},
            "head_sha": "c" * 40,
            "created_at": "2026-09-30T01:00:00Z",
        }
    )
    assert run.status == "in_progress"
    assert is_phantom({"id": run.run_id, "status": run.status, "created_at": run.created_at}, NOW)


# ----- the threshold is derived from this repository, not remembered -----


def _max_minutes(value: object) -> int:
    """A literal, or the largest literal an expression can choose between."""
    if isinstance(value, int):
        return value
    numbers = [int(n) for n in re.findall(r"\b\d+\b", str(value))]
    # An expression naming no number is a timeout this test cannot measure: refuse.
    assert numbers, f"cannot measure timeout-minutes {value!r}"
    return max(numbers)


def _longest_chain_minutes(path: Path, seen: tuple[Path, ...] = ()) -> int:
    assert path not in seen, f"reusable workflow cycle at {path.name}"
    jobs = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs") or {}
    own: dict[str, int] = {}
    for name, spec in jobs.items():
        uses = str(spec.get("uses", ""))
        if uses.startswith("./.github/workflows/"):
            own[name] = _longest_chain_minutes(REPO / uses[2:], (*seen, path))
        else:
            # GitHub's default when a job sets no timeout-minutes.
            own[name] = _max_minutes(spec.get("timeout-minutes", 360))

    memo: dict[str, int] = {}

    def chain(name: str) -> int:
        if name not in memo:
            needs = jobs[name].get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            memo[name] = own[name] + max((chain(n) for n in needs), default=0)
        return memo[name]

    return max((chain(n) for n in jobs), default=0)


def test_phantom_bound_exceeds_every_workflows_longest_timeout_chain() -> None:
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert len(files) >= 20, "the workflow directory was not found or not read"
    longest = max(_longest_chain_minutes(path) for path in files)
    assert longest >= 100, f"positive control: the 100-minute job was not seen ({longest})"
    assert longest < PHANTOM_AFTER_HOURS * 60, (
        f"a {longest}-minute job chain can legitimately run past the {PHANTOM_AFTER_HOURS} h phantom bound"
    )


def test_phantom_bound_exceeds_every_census_lookback_and_the_measured_run_span() -> None:
    lookbacks: list[int] = []
    for name in ("ci-cost-guard.yml", "stable-evidence.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        found = re.findall(r'LOOKBACK_HOURS: "(\d+)"', text)
        assert found, f"{name} sets no LOOKBACK_HOURS"
        lookbacks.extend(int(h) for h in found)
    assert max(lookbacks) < PHANTOM_AFTER_HOURS, (
        "at or below the lookback, every run the census would hold reads as a phantom"
    )
    assert PHANTOM_AFTER_HOURS > MEASURED_LONGEST_RUN_HOURS
