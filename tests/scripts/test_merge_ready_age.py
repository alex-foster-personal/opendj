"""Synthetic GitHub payloads exercise the real report and observation bookkeeping.

[if] merge readiness changes [then] its observed age resets, [else stop].
"""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import merge_ready_age as report

pytestmark = pytest.mark.requirement("OPS-12200")

REPO = "OWNER/REPO"
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


def pull(number: int = 1, **updates: object) -> dict:
    value = {
        "number": number,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "head": {"sha": "a" * 40},
        "base": {"ref": "main", "sha": "b" * 40},
        "state": "open",
        "draft": False,
        "merged": False,
        "created_at": "2020-01-01T00:00:00Z",
    }
    return value | updates


def gate(**updates: object) -> dict:
    return {
        "name": "ci gate",
        "app": {"slug": "github-actions"},
        "id": 10,
        "check_suite": {"id": 20},
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-11-10T10:00:00Z",
        "completed_at": "2026-11-10T11:00:00Z",
    } | updates


class GitHubBoundary:
    """Replace only subprocess I/O, keeping transport parsing and commands real."""

    def __init__(self) -> None:
        self.pulls = [pull()]
        self.checks = [gate()]
        self.triage_exit = 0
        self.scope_exit = 0
        self.triage_updates: dict = {}
        self.invalid_json = False
        self.timeout = False
        self.timeout_module: str | None = None
        self.final_pull: dict | None = None
        self.final_checks: list[dict] | None = None
        self.calls: list[tuple[list[str], dict]] = []
        self.check_reads = 0

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if self.timeout or self.timeout_module in argv:
            raise subprocess.TimeoutExpired(argv, 60)
        data: object
        if "api" in argv:
            endpoint = argv[2]
            if "/check-runs" in endpoint:
                self.check_reads += 1
                checks = self.final_checks if self.check_reads % 2 == 0 else None
                checks = self.checks if checks is None else checks
                data = [
                    {"total_count": len(checks), "check_runs": checks[i : i + 100]}
                    for i in range(0, max(1, len(checks)), 100)
                ]
            elif "?" in endpoint:
                data = [self.pulls[i : i + 100] for i in range(0, len(self.pulls), 100)]
                data = data or [[]]
            else:
                number = int(endpoint.rsplit("/", 1)[1])
                data = self.final_pull or next(p for p in self.pulls if p["number"] == number)
            assert "--method" not in argv
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        if "scripts.review_thread_triage" in argv:
            number = int(argv[3])
            value = {
                "number": number,
                "head_sha": "a" * 40,
                "base_ref": "main",
                "state": "OPEN",
                "merged": False,
                "failing": int(self.triage_exit == 1),
                "debt_file_error": None,
                "carried_error": None,
                "carried_findings": [],
            } | self.triage_updates
            return subprocess.CompletedProcess(
                argv, self.triage_exit, "bad json" if self.invalid_json else json.dumps(value), ""
            )
        assert "scripts.pr_scope_check" in argv
        assert "--comment" not in argv
        return subprocess.CompletedProcess(argv, self.scope_exit, "scope verdict", "")


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> GitHubBoundary:
    value = GitHubBoundary()
    monkeypatch.setattr(report.subprocess, "run", value)
    monkeypatch.setattr(report.shutil, "which", lambda name: name)
    return value


def run_report(tmp_path: Path, when: datetime = NOW, pr: int | None = None) -> report.Report:
    return report.build_report(REPO, tmp_path / "state.json", pr=pr, clock=lambda: when)


def test_age_starts_at_observation_and_threshold(tmp_path: Path, boundary: GitHubBoundary) -> None:
    first = run_report(tmp_path)
    row = first["rows"][0]
    assert row["status"] == "ready"
    assert row["ready_since_observed_at"] == NOW.isoformat()
    assert row["ready_age_seconds"] == 0
    assert first["age_basis"] == "observed"
    for seconds in (7199, 7200, 7201):
        value = run_report(tmp_path, NOW + timedelta(seconds=seconds))
        assert value["rows"][0]["ready_age_seconds"] == seconds
        assert value["rows"][0]["overdue"] is (seconds > 7200)
        assert report.exit_code(value) == int(seconds > 7200)
    assert "OVERDUE" in report.render_text(value)


def test_text_includes_metadata_and_counts_for_empty_report(tmp_path: Path, boundary: GitHubBoundary) -> None:
    boundary.pulls = []
    value = run_report(tmp_path)
    value["schema_version"] = 2
    value["threshold_seconds"] = 42
    text = report.render_text(value)
    assert f"{REPO} at {value['reported_at']}" in text
    assert "schema_version=2" in text
    assert "age_basis=observed" in text
    assert "over 42s is overdue" in text
    for key, count in value["counts"].items():
        assert f"{key}={count}" in text
    assert "Age starts at first observed success" in text


@pytest.mark.parametrize("status", ["ready", "overdue", "not_ready", "unknown"])
def test_text_preserves_row_fields(tmp_path: Path, boundary: GitHubBoundary, status: str) -> None:
    run_report(tmp_path)
    boundary.pulls.append(pull(2, draft=True))
    if status == "not_ready":
        boundary.triage_exit = 1
    elif status == "unknown":
        boundary.triage_exit = 3
    when = NOW + timedelta(seconds=7201) if status == "overdue" else NOW
    value = run_report(tmp_path, when)
    # Formatter input is synthetic; reasons must remain visible even on an overdue row.
    value["rows"][0]["reasons"].append("sample detail")
    text = report.render_text(value)
    for key, count in value["counts"].items():
        assert f"{key}={count}" in text
    for row in value["rows"]:
        summary = " | ".join(
            str(cell) if cell is not None else "-"
            for cell in (
                row["number"],
                row["status"],
                row["ci_status"],
                row["triage_exit"],
                row["scope_exit"],
                row["ready_since_observed_at"],
                row["ready_age_seconds"],
                "OVERDUE" if row["overdue"] else "false",
                "; ".join(row["reasons"]),
            )
        )
        assert summary in text
        evidence = text.split(f"PR #{row['number']} evidence:\n", 1)[1].split("\n", 1)[0]
        for key in (
            "url",
            "head_sha",
            "base_ref",
            "gate_id",
            "gate_suite_id",
            "gate_started_at",
            "gate_completed_at",
            "observed_at",
        ):
            expected = row[key] if row[key] is not None else "-"
            assert f"{key}={expected}" in evidence


@pytest.mark.parametrize("conclusion", [None, "failure", "cancelled", "skipped", "neutral"])
def test_unsuccessful_gate_clears_age(tmp_path: Path, boundary: GitHubBoundary, conclusion: str) -> None:
    run_report(tmp_path)
    boundary.checks = [gate(conclusion=conclusion)]
    row = run_report(tmp_path)["rows"][0]
    assert row["status"] == "not_ready"
    assert row["ready_age_seconds"] is None
    assert not row["overdue"]
    boundary.checks = [gate()]
    assert run_report(tmp_path, NOW + timedelta(hours=3))["rows"][0]["ready_age_seconds"] == 0


def test_new_pending_suite_wins_and_missing_gate(tmp_path: Path, boundary: GitHubBoundary) -> None:
    boundary.checks = (
        [gate(started_at="2026-11-11T11:00:00Z")]
        + [gate(name=f"other {i}", id=30 + i) for i in range(100)]
        + [gate(id=200, check_suite={"id": 21}, started_at=None, status="queued", conclusion=None)]
    )
    row = run_report(tmp_path)["rows"][0]
    assert row["status"] == "not_ready"
    assert row["gate_id"] == 200
    boundary.checks = []
    assert run_report(tmp_path)["rows"][0]["ci_status"] == "missing"


@pytest.mark.parametrize(
    "field,code,status",
    [
        ("triage_exit", 1, "not_ready"),
        ("scope_exit", 1, "not_ready"),
        ("triage_exit", 3, "unknown"),
        ("scope_exit", 3, "unknown"),
        ("triage_exit", 99, "unknown"),
        ("scope_exit", 99, "unknown"),
    ],
)
def test_verdict_exits(tmp_path: Path, boundary: GitHubBoundary, field: str, code: int, status: str) -> None:
    setattr(boundary, field, code)
    value = run_report(tmp_path)
    assert value["rows"][0]["status"] == status
    assert value["rows"][0]["ready_age_seconds"] is None
    assert report.exit_code(value) == (3 if status == "unknown" else 0)


@pytest.mark.parametrize(
    "updates",
    [
        {"head_sha": "wrong"},
        {"number": True},
        {"number": 2},
        {"state": "CLOSED"},
        {"merged": True},
        {"failing": 1},
        {"debt_file_error": "missing"},
        {"debt_file_error": []},
        {"carried_error": {}},
        {"carried_findings": [{}]},
        {"base_ref": "other"},
    ],
)
def test_inconsistent_triage_is_unknown(tmp_path: Path, boundary: GitHubBoundary, updates: dict) -> None:
    boundary.triage_updates = updates
    assert run_report(tmp_path)["rows"][0]["status"] == "unknown"


def test_invalid_json_and_timeout(tmp_path: Path, boundary: GitHubBoundary) -> None:
    boundary.invalid_json = True
    assert run_report(tmp_path)["rows"][0]["status"] == "unknown"
    before = (tmp_path / "state.json").read_bytes()
    boundary.timeout = True
    with pytest.raises(report.MeasureError):
        run_report(tmp_path)
    assert (tmp_path / "state.json").read_bytes() == before


@pytest.mark.parametrize("change", ["head", "base", "gate", "unknown", "draft"])
def test_episode_reset(tmp_path: Path, boundary: GitHubBoundary, change: str) -> None:
    run_report(tmp_path)
    if change == "head":
        boundary.pulls[0]["head"]["sha"] = "c" * 40
        boundary.checks[0]["head_sha"] = "c" * 40
        boundary.triage_updates["head_sha"] = "c" * 40
    elif change == "base":
        boundary.pulls[0]["base"]["ref"] = "develop"
        boundary.triage_updates["base_ref"] = "develop"
    elif change == "gate":
        boundary.checks[0]["id"] += 1
    elif change == "unknown":
        boundary.invalid_json = True
        run_report(tmp_path, NOW + timedelta(seconds=1))
        boundary.invalid_json = False
    elif change == "draft":
        boundary.pulls[0]["draft"] = True
        assert run_report(tmp_path)["rows"][0]["status"] == "not_ready"
        boundary.pulls[0]["draft"] = False
    assert run_report(tmp_path, NOW + timedelta(hours=3))["rows"][0]["ready_age_seconds"] == 0


@pytest.mark.parametrize("change", ["head", "base_sha", "draft", "closed", "gate"])
def test_races_are_unknown(tmp_path: Path, boundary: GitHubBoundary, change: str) -> None:
    boundary.final_pull = copy.deepcopy(pull())
    if change == "head":
        boundary.final_pull["head"]["sha"] = "c" * 40
    elif change == "base_sha":
        boundary.final_pull["base"]["sha"] = "c" * 40
    elif change == "draft":
        boundary.final_pull["draft"] = True
    elif change == "closed":
        boundary.final_pull["state"] = "closed"
    elif change == "gate":
        boundary.final_checks = [gate(id=11)]
    assert run_report(tmp_path)["rows"][0]["status"] == "unknown"


def test_base_advance_between_samples_preserves_age(tmp_path: Path, boundary: GitHubBoundary) -> None:
    run_report(tmp_path)
    boundary.pulls[0]["base"]["sha"] = "c" * 40
    assert run_report(tmp_path, NOW + timedelta(seconds=60))["rows"][0]["ready_age_seconds"] == 60


def test_filtered_scan_preserves_and_full_scan_prunes(tmp_path: Path, boundary: GitHubBoundary) -> None:
    boundary.pulls = [pull(1), pull(2)]
    run_report(tmp_path)
    boundary.pulls = [pull(1)]
    run_report(tmp_path, pr=1)
    assert set(json.loads((tmp_path / "state.json").read_text())["pulls"]) == {"1", "2"}
    run_report(tmp_path)
    assert set(json.loads((tmp_path / "state.json").read_text())["pulls"]) == {"1"}
    boundary.pulls = []
    assert run_report(tmp_path)["rows"] == []


@pytest.mark.parametrize("damage", ["corrupt", "repo", "version", "future", "naive"])
def test_invalid_state_fails(tmp_path: Path, boundary: GitHubBoundary, damage: str) -> None:
    run_report(tmp_path)
    path = tmp_path / "state.json"
    saved = json.loads(path.read_text())
    if damage == "corrupt":
        path.write_text("{", encoding="utf-8")
    else:
        if damage == "repo":
            saved["repository"] = "OTHER/REPO"
        elif damage == "version":
            saved["schema_version"] = 100
        elif damage == "future":
            saved["pulls"]["1"]["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
        elif damage == "naive":
            saved["pulls"]["1"]["ready_since_observed_at"] = "2026-11-11T00:00:00"
        path.write_text(json.dumps(saved), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(report.MeasureError):
        run_report(tmp_path)
    assert path.read_bytes() == before


def test_failed_replace_retains_state(
    tmp_path: Path, boundary: GitHubBoundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_report(tmp_path)
    before = (tmp_path / "state.json").read_bytes()

    def fail_replace(*args: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(report.os, "replace", fail_replace)
    with pytest.raises(OSError):
        run_report(tmp_path, NOW + timedelta(seconds=30))
    assert (tmp_path / "state.json").read_bytes() == before
    assert list(tmp_path.iterdir()) == [tmp_path / "state.json"]


def test_main_and_read_only_repo_propagation(
    tmp_path: Path, boundary: GitHubBoundary, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--repo", REPO, "--state-file", str(tmp_path / "state.json"), "--pr", "1", "--json"]
    assert report.main(args) == 0
    assert json.loads(capsys.readouterr().out)["rows"][0]["status"] == "ready"
    for argv, kwargs in boundary.calls:
        assert kwargs["timeout"] > 0
        if "api" in argv:
            assert argv[2].startswith(f"repos/{REPO}/")
            if "?" in argv[2]:
                assert "--paginate" in argv and "--slurp" in argv
        else:
            assert kwargs["env"]["GH_REPO"] == REPO
            assert argv[argv.index("--owner") + 1] == "OWNER"
            assert argv[argv.index("--repo") + 1] == "REPO"
    boundary.timeout = True
    assert report.main(args) == 3
    assert "UNAVAILABLE" in capsys.readouterr().err


def test_overdue_cli_and_unknown_priority(
    tmp_path: Path,
    boundary: GitHubBoundary,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = ["--repo", REPO, "--state-file", str(tmp_path / "state.json"), "--json"]
    assert report.main(args, clock=lambda: NOW) == 0
    capsys.readouterr()
    later = NOW + timedelta(seconds=7201)
    assert report.main(args, clock=lambda: later) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["rows"][0]["overdue"]
    boundary.pulls.append(pull(2))
    boundary.final_checks = [gate(id=11)]
    # PR 1 remains on its stable gate; PR 2 moves during its final read.
    boundary.check_reads = 0
    original = boundary.__call__

    def transport(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        boundary.final_checks = [gate(id=11)] if boundary.check_reads >= 2 else None
        return original(argv, **kwargs)

    monkeypatch.setattr(report.subprocess, "run", transport)
    assert report.main(args, clock=lambda: later) == 3
    value = json.loads(capsys.readouterr().out)
    assert value["counts"]["overdue"] == value["counts"]["unknown"] == 1


def test_pagination_sorting_and_ties(tmp_path: Path, boundary: GitHubBoundary) -> None:
    boundary.pulls = [pull(number) for number in range(105, 0, -1)]
    value = run_report(tmp_path)
    assert [row["number"] for row in value["rows"]] == list(range(1, 106))
    boundary.pulls.append(pull(106))
    value = run_report(tmp_path, NOW + timedelta(hours=3))
    assert value["rows"][-1]["number"] == 106
    assert value["rows"][-1]["ready_age_seconds"] == 0
    assert value["counts"]["overdue"] == 105


@pytest.mark.parametrize("completed", [None, "bad", "2026-11-11T13:00:00Z", "2026-11-10T09:00:00Z"])
def test_invalid_gate_time_does_not_poison_history(tmp_path: Path, boundary: GitHubBoundary, completed: str) -> None:
    boundary.checks = [gate(completed_at=completed)]
    assert run_report(tmp_path)["rows"][0]["status"] == "unknown"
    boundary.checks = [gate()]
    assert run_report(tmp_path)["rows"][0]["ready_age_seconds"] == 0


def test_timezone_normalization_and_regressing_clock(tmp_path: Path, boundary: GitHubBoundary) -> None:
    run_report(tmp_path, datetime.fromisoformat("2026-11-11T13:00:00+01:00"))
    assert run_report(tmp_path, NOW + timedelta(seconds=5))["rows"][0]["ready_age_seconds"] == 5
    before = (tmp_path / "state.json").read_bytes()
    with pytest.raises(report.MeasureError):
        run_report(tmp_path, NOW)
    assert (tmp_path / "state.json").read_bytes() == before


def test_pending_rerun_in_same_suite(tmp_path: Path, boundary: GitHubBoundary) -> None:
    boundary.checks = [gate(), gate(id=11, started_at=None, status="queued", conclusion=None)]
    row = run_report(tmp_path)["rows"][0]
    assert row["gate_id"] == 11
    assert row["status"] == "not_ready"
    assert not any("scripts.review_thread_triage" in argv for argv, _ in boundary.calls)


@pytest.mark.parametrize("module", ["scripts.review_thread_triage", "scripts.pr_scope_check"])
def test_child_timeout_clears_ready_episode(tmp_path: Path, boundary: GitHubBoundary, module: str) -> None:
    run_report(tmp_path)
    boundary.timeout_module = module
    value = run_report(tmp_path, NOW + timedelta(seconds=1))
    assert report.exit_code(value) == 3
    assert value["rows"][0]["status"] == "unknown"
    assert value["rows"][0]["ready_since_observed_at"] is None
    boundary.timeout_module = None
    assert run_report(tmp_path, NOW + timedelta(hours=3))["rows"][0]["ready_age_seconds"] == 0


def test_gate_timestamp_offsets_preserve_episode(tmp_path: Path, boundary: GitHubBoundary) -> None:
    run_report(tmp_path)
    boundary.checks = [gate(started_at="2026-11-10T11:00:00+01:00", completed_at="2026-11-10T12:00:00+01:00")]
    assert run_report(tmp_path, NOW + timedelta(seconds=60))["rows"][0]["ready_age_seconds"] == 60


def test_gate_from_another_app_is_unknown(tmp_path: Path, boundary: GitHubBoundary) -> None:
    boundary.checks = [gate(app={"slug": "other-app"})]
    assert run_report(tmp_path)["rows"][0]["status"] == "unknown"
