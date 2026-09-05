"""Acceptance tests for REDTEAM-04 dedupe and issue filing."""

from __future__ import annotations

from pathlib import Path

from scripts import redteam_filing as mod
from scripts import redteam_findings as findings

SHA = "a" * 40


def _finding(*, fingerprint: str = "deck-1-silent", host: str = "agentbox-01") -> findings.Finding:
    return findings.Finding.fail(
        details=findings.FindingDetails(
            fingerprint=fingerprint,
            sha=SHA,
            surface="area:performance-screen",
            host=host,
            repro=findings.Reproduction(
                act_sequence=("load deck 1", "press play"), trace_session="trace-847"
            ),
            evidence=findings.Evidence(trace_url="https://trace.example/847", screenshot=None),
            honest_coverage="Chromium production build on agentbox.",
        )
    )


class RecordingGitHub:
    """A protocol implementation that records the GitHub actions requested."""

    def __init__(self, matches: dict[str, int] | None = None) -> None:
        self.matches = {} if matches is None else matches
        self.created_labels: list[str] = []
        self.created_issues: list[tuple[str, str, tuple[str, ...]]] = []
        self.comments: list[tuple[int, str]] = []

    def find_open_bug_issue(self, fingerprint: str) -> int | None:
        return self.matches.get(fingerprint)

    def ensure_label(self, name: str, description: str) -> None:
        self.created_labels.append(name)

    def create_issue(self, title: str, body: str, labels: tuple[str, ...]) -> int:
        self.created_issues.append((title, body, labels))
        return 900 + len(self.created_issues)

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))


def test_same_fault_from_two_pods_files_exactly_one_issue() -> None:
    """If the same fault is found by two pods then exactly one issue is filed, or broken."""
    github = RecordingGitHub()

    result = mod.file_findings(
        (_finding(), _finding(host="bifrost2")),
        run_id="20260905-0700",
        priority="p1",
        github=github,
    )

    assert result.created == (901,)
    assert result.commented == ()
    assert len(github.created_issues) == 1
    assert tuple(github.created_labels) == ("redteam", "redteam-run:20260905-0700")
    _title, body, labels = github.created_issues[0]
    assert mod.fingerprint_footer("deck-1-silent") in body
    assert labels == (
        "bug",
        "redteam",
        "redteam-run:20260905-0700",
        "queue:ready",
        "queue:p1",
        "area:performance-screen",
    )


def test_existing_open_bug_issue_receives_a_seen_again_comment() -> None:
    """If the fault has an open issue then the run comments instead of duplicating, or broken."""
    github = RecordingGitHub({"deck-1-silent": 8470})

    result = mod.file_findings((_finding(),), run_id="20260905-0700", priority="p0", github=github)

    assert result.created == ()
    assert result.commented == (8470,)
    assert github.created_issues == []
    assert github.comments == [(8470, f"seen again at {SHA}, run 20260905-0700")]


def test_unavailable_findings_do_not_file_bug_issues() -> None:
    """If a pod reports UNAVAILABLE then no fault issue is filed, or broken."""
    unavailable = findings.Finding.unavailable(
        details=findings.FindingDetails(
            fingerprint="windows-audio-unavailable",
            sha=SHA,
            surface="area:performance-screen",
            host="bifrost2",
            repro=findings.Reproduction(
                act_sequence=("open app",), trace_session="trace-unavailable"
            ),
            evidence=findings.Evidence(
                trace_url="https://trace.example/unavailable", screenshot=None
            ),
            honest_coverage="No output devices were exposed.",
        )
    )
    github = RecordingGitHub()

    result = mod.file_findings((unavailable,), run_id="20260905-0700", priority="p1", github=github)

    assert result.created == ()
    assert result.commented == ()
    assert github.created_issues == []


def test_open_bug_dedupe_does_not_depend_on_eventual_search_index() -> None:
    """If an issue was just filed then a later run lists its open body directly, or broken."""
    source = Path(mod.__file__).read_text(encoding="utf-8")

    assert (
        "repos/{self._repository}/issues?state=open&labels=bug&per_page=100&page={page}" in source
    )
    assert '"--limit", "1000"' not in source
    assert '"--search", footer' not in source
