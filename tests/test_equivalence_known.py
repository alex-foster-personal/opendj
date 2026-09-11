"""apps/equivalence/known.py review thread (PR #383, BLOCKING):
KnownAnswerReport.ok must not treat a pending (needs-maintainer) check as a pass.

Before this fix, ``ok`` only looked at ``failed`` and ``unresolved_checks``,
so a fixture with a checked-in ``needs-maintainer`` entry (expected value legitimately
unknown, pending a human ear) still reported ``ok=True``. That let
``apps.equivalence run`` publish the canonical gate file and exit 0 even though
its own CLI contract requires every known-answer check to pass and this one
explicitly never ran to a verdict.
"""
from __future__ import annotations

from apps.equivalence.known import CheckResult, KnownAnswerReport


def _report(**status_counts: int) -> KnownAnswerReport:
    results: list[CheckResult] = []
    for status, count in status_counts.items():
        results.extend(
            CheckResult(
                track_id=f"{status}-{i}",
                target="mik.bpm",
                expected_raw=None,
                actual_raw=None,
                expected_canonical=None,
                actual_canonical=None,
                verified_by="needs-maintainer" if status == "pending" else "source-read",
                status=status,
            )
            for i in range(count)
        )
    return KnownAnswerReport(
        fixture_path="fake-fixture.json",
        tracks=sum(status_counts.values()),
        passed=status_counts.get("pass", 0),
        failed=status_counts.get("fail", 0),
        pending=status_counts.get("pending", 0),
        unresolved=0,
        results=results,
    )


def test_ok_when_everything_passed() -> None:
    assert _report(**{"pass": 3}).ok is True


def test_not_ok_when_a_check_is_pending() -> None:
    """A checked-in needs-maintainer entry (pending) must block a trustworthy verdict."""
    report = _report(**{"pass": 9, "pending": 1})
    assert report.failed == 0
    assert report.unresolved_checks == 0
    assert report.ok is False


def test_not_ok_when_a_check_failed() -> None:
    assert _report(**{"pass": 9, "fail": 1}).ok is False


def test_not_ok_when_a_check_is_unresolved() -> None:
    report = _report(**{"pass": 9})
    report.results.append(
        CheckResult(
            track_id="missing",
            target="mik.bpm",
            expected_raw=None,
            actual_raw=None,
            expected_canonical=None,
            actual_canonical=None,
            verified_by="source-read",
            status="unresolved",
        )
    )
    assert report.unresolved_checks == 1
    assert report.ok is False
