"""Deduplicate REDTEAM-03 findings and file or update their GitHub issues.

Requirements:
    - [if] the same fault is found by two pods in one run [then] exactly one issue is filed
    - [if] an open issue carries the fault [then] the run comments, rather than duplicates it
    - [if] a finding is UNAVAILABLE [then] no bug issue is filed for it

The caller supplies both the run id and the priority explicitly. This module
reads the immutable REDTEAM-03 ledger and never changes a pod finding.

-Codex
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

try:
    from scripts.redteam_findings import Finding, FindingStore, Verdict
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.redteam_filing") from None
    raise

DEFAULT_REPOSITORY = "private_owner/music-dj-tools"
LABEL_COLOR = "b60205"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class GitHub(Protocol):
    """The small GitHub surface REDTEAM-04 needs to make filing deterministic."""

    def find_open_bug_issue(self, fingerprint: str) -> int | None: ...

    def ensure_label(self, name: str, description: str) -> None: ...

    def create_issue(self, title: str, body: str, labels: tuple[str, ...]) -> int: ...

    def comment(self, number: int, body: str) -> None: ...


class GhGitHub:
    """GitHub CLI implementation which searches bodies for the exact footer."""

    def __init__(self, repository: str) -> None:
        self._repository = repository

    def find_open_bug_issue(self, fingerprint: str) -> int | None:
        footer = fingerprint_footer(fingerprint)
        matches: list[int] = []
        page = 1
        while True:
            issues = json.loads(
                self._api(
                    f"repos/{self._repository}/issues?state=open&labels=bug&per_page=100&page={page}"
                )
            )
            if not issues:
                break
            matches.extend(
                issue["number"]
                for issue in issues
                if "pull_request" not in issue and footer in issue["body"]
            )
            page += 1
        if len(matches) > 1:
            raise RuntimeError(
                f"multiple open bug issues carry fingerprint {fingerprint!r}: {matches}"
            )
        return matches[0] if matches else None

    def ensure_label(self, name: str, description: str) -> None:
        output = self._run("label", "list", "--search", name, "--limit", "100", "--json", "name")
        labels = json.loads(output)
        if any(label["name"] == name for label in labels):
            return
        self._run("label", "create", name, "--color", LABEL_COLOR, "--description", description)

    def create_issue(self, title: str, body: str, labels: tuple[str, ...]) -> int:
        arguments = ["issue", "create", "--title", title, "--body", body]
        for label in labels:
            arguments.extend(("--label", label))
        url = self._run(*arguments).strip()
        number = url.rsplit("/", maxsplit=1)[-1]
        if not number.isdecimal():
            raise RuntimeError(f"gh issue create returned an unrecognizable URL: {url!r}")
        return int(number)

    def comment(self, number: int, body: str) -> None:
        self._run("issue", "comment", str(number), "--body", body)

    def _run(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["gh", *arguments, "--repo", self._repository],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    def _api(self, endpoint: str) -> str:
        completed = subprocess.run(
            ["gh", "api", endpoint], check=True, capture_output=True, text=True
        )
        return completed.stdout


@dataclass(frozen=True)
class FilingResult:
    """The issues created or updated by one run, in finding order."""

    created: tuple[int, ...]
    commented: tuple[int, ...]


def fingerprint_footer(fingerprint: str) -> str:
    """Return the exact, machine-searchable footer carried by each red-team issue."""
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ValueError("fingerprint must be a non-empty string")
    if "\n" in fingerprint or "\r" in fingerprint:
        raise ValueError("fingerprint must be one line")
    return f"<!-- redteam-fingerprint: {fingerprint} -->"


def file_findings(
    findings: Iterable[Finding],
    *,
    run_id: str,
    priority: str,
    github: GitHub,
    extra_labels: tuple[str, ...] = (),
) -> FilingResult:
    """File each distinct failed fingerprint once, or add its repeat observation."""
    _validate_run_id(run_id)
    queue_label = _priority_label(priority)
    extras = _validate_extra_labels(extra_labels)
    unique_failures = _unique_failures(findings)
    created: list[int] = []
    commented: list[int] = []
    for finding in unique_failures:
        existing = github.find_open_bug_issue(finding.fingerprint)
        if existing is not None:
            github.comment(existing, f"seen again at {finding.sha}, run {run_id}")
            commented.append(existing)
            continue
        labels = _issue_labels(finding, run_id, queue_label, extras)
        github.ensure_label("redteam", "Red-team fleet finding")
        github.ensure_label(f"redteam-run:{run_id}", f"Red-team fleet run {run_id}")
        for extra in extras:
            github.ensure_label(extra, f"Red-team extra label {extra}")
        created.append(github.create_issue(_issue_title(finding), _issue_body(finding), labels))
    return FilingResult(created=tuple(created), commented=tuple(commented))


def _unique_failures(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Preserve the first FAIL for every fingerprint and ignore availability reports."""
    unique: dict[str, Finding] = {}
    for finding in findings:
        if not isinstance(finding, Finding):
            raise TypeError("findings must contain Finding values")
        if finding.verdict == Verdict.FAIL:
            unique.setdefault(finding.fingerprint, finding)
    return tuple(unique.values())


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must contain only letters, digits, dots, hyphens, and underscores")


def _priority_label(priority: str) -> str:
    if priority not in {"p0", "p1", "p2"}:
        raise ValueError("priority must be p0, p1, or p2")
    return f"queue:{priority}"


def _validate_extra_labels(extra_labels: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(extra_labels, tuple):
        raise TypeError("extra_labels must be a tuple of strings")
    cleaned: list[str] = []
    for label in extra_labels:
        if not isinstance(label, str) or not label.strip():
            raise ValueError("extra labels must be non-empty strings")
        if "\n" in label or "\r" in label:
            raise ValueError("extra labels must be one line")
        if label not in cleaned:
            cleaned.append(label)
    return tuple(cleaned)


def _issue_labels(
    finding: Finding, run_id: str, queue_label: str, extra_labels: tuple[str, ...] = ()
) -> tuple[str, ...]:
    return (
        "bug",
        "redteam",
        f"redteam-run:{run_id}",
        "queue:ready",
        queue_label,
        finding.surface,
        *extra_labels,
    )


def _issue_title(finding: Finding) -> str:
    return f"redteam: {finding.fingerprint}"


def _issue_body(finding: Finding) -> str:
    screenshot = finding.evidence.screenshot or "Not captured"
    actions = "\n".join(
        f"{index}. {action}" for index, action in enumerate(finding.repro.act_sequence, 1)
    )
    return "\n".join(
        (
            "## Reproduction plan",
            "",
            actions,
            "",
            f"Trace session: {finding.repro.trace_session}",
            f"Trace: {finding.evidence.trace_url}",
            f"Screenshot: {screenshot}",
            "",
            "## Coverage",
            "",
            finding.honest_coverage,
            "",
            fingerprint_footer(finding.fingerprint),
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path, help="REDTEAM-03 run index.jsonl")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--priority", required=True, choices=("p0", "p1", "p2"))
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        dest="labels",
        help="Extra labels on created issues (repeatable). Track (g) uses --label red-team.",
    )
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    arguments = parser.parse_args()
    result = file_findings(
        FindingStore(arguments.index).read_all(),
        run_id=arguments.run_id,
        priority=arguments.priority,
        github=GhGitHub(arguments.repo),
        extra_labels=tuple(arguments.labels),
    )
    print(json.dumps({"created": result.created, "commented": result.commented}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
