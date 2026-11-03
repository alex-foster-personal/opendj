"""Placeholder Sentry probes must not be treated as production stacks.

[if] TEST-SENTRY / test-project / missing stack arrives [then] not actionable, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from apps.shared.telemetry.sentry_issue_intake import (
    SentryIssueRef,
    classify_sentry_issue,
    is_placeholder_sentry_issue,
    main,
)

ROOT = Path(__file__).resolve().parents[2]


def _probe() -> SentryIssueRef:
    return SentryIssueRef(
        issue_id="12345",
        short_id="TEST-SENTRY",
        title="Test Sentry issue",
        project_slug="test-project",
        issue_url="https://sentry.io/issues/12345/",
    )


def test_test_sentry_short_id_is_a_placeholder() -> None:
    """[if] short_id starts with TEST- [then] placeholder, [else stop]."""
    ref = _probe()
    assert is_placeholder_sentry_issue(ref) is True
    verdict = classify_sentry_issue(ref)
    assert verdict.actionable is False
    assert verdict.kind == "placeholder"


def test_real_open_dj_issue_with_a_stack_is_actionable() -> None:
    """[if] OPEN-DJ-BE short id plus exception type [then] actionable, [else stop]."""
    ref = SentryIssueRef(
        issue_id="987654321",
        short_id="OPEN-DJ-BE-11",
        title="ZeroDivisionError: division by zero",
        project_slug="open-dj-be",
        exception_type="ZeroDivisionError",
        culprit="apps.webui.server.routes.health",
    )
    assert is_placeholder_sentry_issue(ref) is False
    verdict = classify_sentry_issue(ref)
    assert verdict.actionable is True
    assert verdict.kind == "actionable"


def test_real_id_without_stack_is_missing_environment() -> None:
    """[if] a real short id has no exception or culprit [then] missing_stack, [else stop]."""
    ref = SentryIssueRef(
        issue_id="2680",
        short_id="OPEN-DJ-BE-12",
        title="client error forwarded",
        project_slug="open-dj-be",
    )
    verdict = classify_sentry_issue(ref)
    assert verdict.actionable is False
    assert verdict.kind == "missing_stack"


def test_cli_exits_2_for_the_cursor_test_probe() -> None:
    """[if] the TEST-SENTRY CLI args are passed [then] exit 2 and JSON kind, [else stop]."""
    code = main(
        [
            "--issue-id",
            "12345",
            "--short-id",
            "TEST-SENTRY",
            "--title",
            "Test Sentry issue",
            "--project-slug",
            "test-project",
        ]
    )
    assert code == 2


def test_module_cli_subprocess_prints_json() -> None:
    """[if] python -m the intake module on TEST-SENTRY [then] JSON placeholder, [else stop]."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.shared.telemetry.sentry_issue_intake",
            "--issue-id",
            "12345",
            "--short-id",
            "TEST-SENTRY",
            "--title",
            "Test Sentry issue",
            "--project-slug",
            "test-project",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["verdict"]["kind"] == "placeholder"
    assert payload["verdict"]["actionable"] is False
