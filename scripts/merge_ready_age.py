"""Report observed merge-ready PR age, independently of open-PR age (issue #12200).

Read-only GitHub collection; only the explicitly selected local state file is written.
Readiness requires a successful current-head ci gate and passing triage and scope checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Literal, TypedDict

from scripts.ci_wait_core import _latest_by_name, _merge_check_run_pages
from scripts.review_gh import TriageError, _resolve_repo

SCHEMA_VERSION = 1
OVERDUE_SECONDS = 7200
PROCESS_TIMEOUT_SECONDS = 120
Clock = Callable[[], datetime]


class Row(TypedDict):
    number: int
    url: str
    head_sha: str
    base_ref: str
    status: Literal["ready", "not_ready", "unknown"]
    ci_status: str | None
    triage_exit: int | None
    scope_exit: int | None
    reasons: list[str]
    gate_id: int | None
    gate_completed_at: str | None
    gate_started_at: str | None
    gate_suite_id: int | None
    ready_since_observed_at: str | None
    ready_age_seconds: int | None
    overdue: bool
    observed_at: str


class Report(TypedDict):
    schema_version: int
    repository: str
    reported_at: str
    threshold_seconds: int
    age_basis: Literal["observed"]
    counts: dict[str, int]
    rows: list[Row]


class MeasureError(RuntimeError):
    """Unavailable or inconsistent evidence, never a passing verdict."""


def timestamp(value: str) -> datetime:
    """Require timezone-aware evidence and normalize offsets to UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MeasureError(f"timestamp requires a timezone: {value!r}")
    return parsed.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _clock_time(clock: Clock) -> datetime:
    return timestamp(clock().isoformat())


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=PROCESS_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MeasureError(f"{Path(argv[0]).name} unavailable: {str(exc)[:400]}") from exc


class GitHub:
    """Bounded read-only transport, reusing the CI waiter's pagination/rerun policy."""

    def __init__(self, repository: str) -> None:
        self.repository = repository
        executable = shutil.which("gh")
        if executable is None:
            raise MeasureError("gh is not installed")
        self.gh = executable

    def read(self, endpoint: str, *, pages: bool = False) -> object:
        argv = [self.gh, "api", f"repos/{self.repository}/{endpoint}"]
        if pages:
            argv.extend(["--paginate", "--slurp"])
        result = _run(argv)
        if result.returncode != 0:
            raise MeasureError(f"GitHub read exited {result.returncode}: {result.stderr[:400]}")
        return json.loads(result.stdout)

    def pulls(self, number: int | None) -> list[dict]:
        if number is not None:
            return [_validate_pull(self.read(f"pulls/{number}"))]
        pages = self.read("pulls?state=open&per_page=100", pages=True)
        if not isinstance(pages, list) or not pages or any(not isinstance(p, list) for p in pages):
            raise MeasureError("invalid pull request pages")
        pulls = [_validate_pull(pull) for page in pages for pull in page]
        if len({p["number"] for p in pulls}) != len(pulls):
            raise MeasureError("duplicate pull requests across pages")
        return pulls

    def gate(self, head: str) -> dict | None:
        pages = self.read(f"commits/{head}/check-runs?per_page=100&filter=all", pages=True)
        if not isinstance(pages, list) or not pages:
            raise MeasureError("invalid check-run pages")
        if any(page["total_count"] != pages[0]["total_count"] for page in pages):
            raise MeasureError("check-run count changed during pagination")
        checks = _merge_check_run_pages(pages)
        gates = [check for check in checks if check["name"] == "ci gate"]
        # A queued rerun can have no start time. Do not let an older success in
        # the same suite hide it (the shared ordering otherwise compares None).
        ordered = [dict(check, started_at=check.get("started_at") or "") for check in gates]
        selected = _latest_by_name(ordered).get("ci gate")
        if selected is not None:
            suite = (selected.get("check_suite") or {}).get("id")
            queued = [
                check
                for check in gates
                if not check.get("started_at")
                and (check.get("check_suite") or {}).get("id") == suite
                and check["id"] > selected["id"]
            ]
            selected_id = max([selected["id"], *(check["id"] for check in queued)])
            selected = next(check for check in gates if check["id"] == selected_id)
        if selected is not None and (
            type(selected["id"]) is not int
            or selected["id"] <= 0
            or selected["head_sha"] != head
            or selected["app"]["slug"] != "github-actions"
            or selected["status"] not in {"queued", "in_progress", "completed", "waiting", "requested", "pending"}
        ):
            raise MeasureError("invalid ci gate identity/status")
        return selected

    def run_triage(self, number: int) -> subprocess.CompletedProcess[str]:
        owner, repo = self.repository.split("/")
        argv = [
            sys.executable,
            "-m",
            "scripts.review_thread_triage",
            str(number),
            "--owner",
            owner,
            "--repo",
            repo,
            "--json",
        ]
        return _run(argv, env=dict(os.environ, GH_REPO=self.repository))

    def run_scope_check(self, number: int) -> subprocess.CompletedProcess[str]:
        owner, repo = self.repository.split("/")
        argv = [
            sys.executable,
            "-m",
            "scripts.pr_scope_check",
            str(number),
            "--owner",
            owner,
            "--repo",
            repo,
        ]
        return _run(argv, env=dict(os.environ, GH_REPO=self.repository))


def _validate_pull(value: object) -> dict:
    if not isinstance(value, dict):
        raise MeasureError("invalid pull request object")
    if (
        type(value["number"]) is not int
        or value["number"] <= 0
        or not isinstance(value["html_url"], str)
        or not value["html_url"]
        or not value["head"]["sha"]
        or not value["base"]["sha"]
        or not value["base"]["ref"]
        or value["state"] not in {"open", "closed"}
        or type(value["draft"]) is not bool
    ):
        raise MeasureError("invalid pull request identity")
    return value


def _pull_identity(pull: dict) -> tuple:
    return (
        pull["number"],
        pull["head"]["sha"],
        pull["base"]["ref"],
        pull["base"]["sha"],
        pull["state"],
        pull["draft"],
        pull.get("merged", False),
    )


def _gate_identity(gate: dict | None) -> tuple | None:
    if gate is None:
        return None
    return (
        gate["id"],
        gate.get("check_suite"),
        _normalized_time(gate.get("started_at")),
        gate["status"],
        gate["conclusion"],
        _normalized_time(gate.get("completed_at")),
    )


def _normalized_time(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MeasureError("timestamp must be a string")
    return timestamp(value).isoformat()


def _validate_triage(result: subprocess.CompletedProcess[str], pull: dict) -> None:
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise MeasureError(f"invalid triage JSON (exit {result.returncode}): {result.stderr.strip()[-400:]}") from exc
    if (
        type(value["number"]) is not int
        or value["number"] != pull["number"]
        or value["head_sha"] != pull["head"]["sha"]
        or value["base_ref"] != pull["base"]["ref"]
        or value["state"] != "OPEN"
        or value["merged"] is not False
        or type(value["failing"]) is not int
        or value["failing"] < 0
        or not isinstance(value["carried_findings"], list)
        or (value["debt_file_error"] is not None and not isinstance(value["debt_file_error"], str))
        or (value["carried_error"] is not None and not isinstance(value["carried_error"], str))
    ):
        raise MeasureError("triage identity or verdict is inconsistent")
    blocked = bool(value["failing"] or value["debt_file_error"] or value["carried_findings"])
    if value["carried_error"] or blocked != (result.returncode == 1):
        raise MeasureError("triage exit disagrees with its payload")


def _require_verdict(result: subprocess.CompletedProcess[str], name: str) -> None:
    if result.returncode not in (0, 1):
        raise MeasureError(f"{name} unavailable (exit {result.returncode}): {result.stderr[:400]}")


def _collect_verdicts(github: GitHub, pull: dict, selected: dict, row: Row) -> None:
    triage = github.run_triage(row["number"])
    row["triage_exit"] = triage.returncode
    _require_verdict(triage, "triage")
    _validate_triage(triage, pull)
    scope = github.run_scope_check(row["number"])
    row["scope_exit"] = scope.returncode
    _require_verdict(scope, "scope")
    if triage.returncode == 1:
        row["reasons"].append("review triage blocked")
    if scope.returncode == 1:
        row["reasons"].append("scope check blocked")
    final_pull = github.pulls(row["number"])[0]
    final_gate = github.gate(row["head_sha"])
    if _pull_identity(final_pull) != _pull_identity(pull) or _gate_identity(final_gate) != _gate_identity(selected):
        raise MeasureError("PR identity or ci gate changed during collection")
    if not row["reasons"]:
        row["status"] = "ready"


def _validate_gate_time(selected: dict | None, observed: datetime) -> None:
    if selected is None:
        return
    for key in ("started_at", "completed_at"):
        if selected.get(key) is not None and timestamp(selected[key]) > observed:
            raise MeasureError(f"ci gate {key} is in the future")
    if selected["status"] == "completed" and selected["conclusion"] == "success":
        completed = timestamp(selected["completed_at"])
        if selected.get("started_at") is not None and timestamp(selected["started_at"]) > completed:
            raise MeasureError("ci gate completion precedes its start")


def collect(github: GitHub, pull: dict, clock: Clock) -> Row:
    """Collect one consistent sampled observation; failed measurements clear its clock."""
    row: Row = {
        "number": pull["number"],
        "url": pull["html_url"],
        "head_sha": pull["head"]["sha"],
        "base_ref": pull["base"]["ref"],
        "status": "not_ready",
        "ci_status": "unknown",
        "triage_exit": None,
        "scope_exit": None,
        "reasons": [],
        "gate_id": None,
        "gate_completed_at": None,
        "gate_started_at": None,
        "gate_suite_id": None,
        "ready_since_observed_at": None,
        "ready_age_seconds": None,
        "overdue": False,
        "observed_at": "",
    }
    selected = None
    try:
        if pull["state"] != "open" or pull.get("merged", False):
            row["reasons"].append("closed or merged")
        elif pull["draft"]:
            row["reasons"].append("draft")
        else:
            selected = github.gate(row["head_sha"])
            if selected is None:
                row["ci_status"] = "missing"
            else:
                row.update(
                    {
                        "gate_id": selected["id"],
                        "gate_completed_at": selected.get("completed_at"),
                        "gate_started_at": selected.get("started_at"),
                        "gate_suite_id": (selected.get("check_suite") or {}).get("id"),
                    }
                )
                row["ci_status"] = selected["conclusion"] if selected["status"] == "completed" else selected["status"]
            if selected is None or selected["status"] != "completed" or selected["conclusion"] != "success":
                row["reasons"].append(f"ci gate: {row['ci_status']}")
            else:
                _collect_verdicts(github, pull, selected, row)
    except (MeasureError, TriageError, ValueError, KeyError, TypeError, AttributeError) as exc:
        row["status"] = "unknown"
        row["reasons"].append(f"UNAVAILABLE: {str(exc)[:500]}")
        # Invalid evidence must not poison the next invocation's local history.
        row.update({"gate_completed_at": None, "gate_started_at": None})
    observed = _clock_time(clock)
    try:
        _validate_gate_time(selected, observed)
        row["gate_completed_at"] = _normalized_time(row["gate_completed_at"])
        row["gate_started_at"] = _normalized_time(row["gate_started_at"])
    except (MeasureError, ValueError, KeyError, TypeError, AttributeError) as exc:
        row["status"] = "unknown"
        row["reasons"].append(f"UNAVAILABLE: {str(exc)[:500]}")
        row.update({"gate_completed_at": None, "gate_started_at": None})
    row["observed_at"] = observed.isoformat()
    return row


def _episode_identity(row: Mapping[str, object]) -> tuple:
    return (
        row["head_sha"],
        row["base_ref"],
        row["gate_id"],
        _normalized_time(row["gate_completed_at"]),
        _normalized_time(row["gate_started_at"]),
        row["gate_suite_id"],
    )


def transition(row: Row, previous: Row | None) -> Row:
    """Continue only a still-ready episode with unchanged head, target and gate."""
    observed = timestamp(row["observed_at"])
    if previous is not None and timestamp(previous["observed_at"]) > observed:
        raise MeasureError("observation clock regressed")
    if row["status"] == "ready":
        since = row["observed_at"]
        if (
            previous is not None
            and previous["status"] == "ready"
            and _episode_identity(previous) == _episode_identity(row)
        ):
            saved = previous["ready_since_observed_at"]
            if saved is None:
                raise MeasureError("ready state has no episode start")
            since = saved
        elapsed = (observed - timestamp(since)).total_seconds()
        if elapsed < 0:
            raise MeasureError("ready timestamp is in the future")
        row.update(
            {
                "ready_since_observed_at": since,
                "ready_age_seconds": int(elapsed),
                "overdue": int(elapsed) > OVERDUE_SECONDS,
            }
        )
    return row


def load_state(path: Path, repository: str, now: datetime) -> dict:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "repository": repository, "pulls": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if (
        type(state["schema_version"]) is not int
        or state["schema_version"] != SCHEMA_VERSION
        or state["repository"] != repository
        or not isinstance(state["pulls"], dict)
    ):
        raise MeasureError("state schema or repository mismatch")
    for number, row in state["pulls"].items():
        if str(row["number"]) != number or row["status"] not in {"ready", "not_ready", "unknown"}:
            raise MeasureError("invalid saved PR record")
        _episode_identity(row)
        observed = timestamp(row["observed_at"])
        if observed > now:
            raise MeasureError("clock precedes saved observation")
        since = row["ready_since_observed_at"]
        if (since is not None) != (row["status"] == "ready"):
            raise MeasureError("invalid saved readiness episode")
        if since is not None and timestamp(since) > observed:
            raise MeasureError("saved readiness starts after observation")
        for field in ("gate_completed_at", "gate_started_at"):
            if row[field] is not None and timestamp(row[field]) > observed:
                raise MeasureError("saved gate timestamp is in the future")
    return state


def write_state(path: Path, state: dict) -> None:
    """Atomically replace the selected file and clean up an unsuccessful write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_report(repository: str, state_file: Path, *, pr: int | None = None, clock: Clock = utc_now) -> Report:
    started = _clock_time(clock)
    try:
        state = load_state(state_file, repository, started)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise MeasureError(f"invalid observation state: {exc}") from exc
    github = GitHub(repository)
    pulls = github.pulls(pr)
    rows = [transition(collect(github, pull, clock), state["pulls"].get(str(pull["number"]))) for pull in pulls]
    reported = _clock_time(clock)
    times = [started, *(timestamp(row["observed_at"]) for row in rows), reported]
    if any(after < before for before, after in pairwise(times)):
        raise MeasureError("report clock regressed")
    rows.sort(
        key=lambda row: (
            0 if row["overdue"] else 1 if row["status"] == "ready" else 2,
            -(row["ready_age_seconds"] or 0),
            row["number"],
        )
    )
    counts: Counter[str] = Counter(row["status"] for row in rows)
    counts["overdue"] = sum(row["overdue"] for row in rows)
    result: Report = {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "reported_at": reported.isoformat(),
        "threshold_seconds": OVERDUE_SECONDS,
        "age_basis": "observed",
        "counts": {key: counts[key] for key in ("ready", "not_ready", "unknown", "overdue")},
        "rows": rows,
    }
    records = dict(state["pulls"]) if pr is not None else {}
    records.update({str(row["number"]): row for row in rows})
    write_state(state_file, dict(state, pulls=records))
    return result


def exit_code(result: Report) -> int:
    return 3 if result["counts"]["unknown"] else 1 if result["counts"]["overdue"] else 0


def render_text(result: Report) -> str:
    lines = [
        f"{result['repository']} at {result['reported_at']} - observed readiness age "
        f"(over {result['threshold_seconds']}s is overdue)",
        f"schema_version={result['schema_version']} | age_basis={result['age_basis']}",
        "Counts: " + " | ".join(f"{key}={count}" for key, count in result["counts"].items()),
        "PR | Status | CI | Triage | Scope | Ready since observed | Age seconds | Overdue | Reasons",
        "---|---|---|---|---|---|---|---|---",
    ]
    lines.extend(
        " | ".join(
            str(value) if value is not None else "-"
            for value in (
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
        for row in result["rows"]
    )
    for row in result["rows"]:
        lines.append(f"\nPR #{row['number']} evidence:")
        lines.append(
            " | ".join(
                f"{key}={row[key] if row[key] is not None else '-'}"
                for key in (
                    "url",
                    "head_sha",
                    "base_ref",
                    "gate_id",
                    "gate_suite_id",
                    "gate_started_at",
                    "gate_completed_at",
                    "observed_at",
                )
            )
        )
    lines.append("Age starts at first observed success; changes between samples may be missed.")
    return "\n".join(lines)


def _validate_arguments(repository: str, pr: int | None) -> None:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or (pr is not None and pr <= 0):
        raise MeasureError("expected OWNER/REPO and a positive PR number")


def main(argv: list[str] | None = None, *, clock: Clock = utc_now) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", help="OWNER/REPO (otherwise use the review tooling's repository resolution)")
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--pr", type=int, help="report one PR without pruning other saved records")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        repository = _resolve_repo(args.repo)
        _validate_arguments(repository, args.pr)
        result = build_report(repository, args.state_file, pr=args.pr, clock=clock)
    except (MeasureError, TriageError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"UNAVAILABLE: {str(exc)[:500]}", file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2) if args.json else render_text(result))
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
