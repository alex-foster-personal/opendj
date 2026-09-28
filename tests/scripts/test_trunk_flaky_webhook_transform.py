"""The Trunk Flaky Tests -> GitHub Issues webhook transformation (`ci/trunk/*.transform.js`).

Svix runs the file's `handler(webhook)`; these tests run the same file under node with a
payload shaped like Trunk's documented `v2.test_case.status_changed` event.

Regression lines:
  - if a FLAKY transition is cancelled then new flaky tests never reach the issue tracker
  - if a BROKEN transition is cancelled then consistently failing tests are never ticketed
  - if a HEALTHY transition is sent then every recovery files a fresh, bogus issue
  - if the title can exceed 250 characters then GitHub rejects the create with a 422
  - if the issue body drops the Trunk URL then a reader cannot reach the test's history
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

TRANSFORM = (
    Path(__file__).resolve().parents[2] / "ci" / "trunk" / "flaky-tests-github-issues.transform.js"
)
HARNESS = "\nprocess.stdout.write(JSON.stringify(handler(JSON.parse(process.argv[1]))));\n"


# -----------------------------------------------------------------------------
def _event(new_status: str, name: str = "tests/test_x.py::test_y") -> dict[str, Any]:
    return {
        "eventType": "v2.test_case.status_changed",
        "method": "POST",
        "url": "https://api.github.com/repos/owner/repo/issues",
        "cancel": False,
        "payload": {
            "previous_status": "HEALTHY",
            "new_status": new_status,
            "timestamp": "2026-09-28T07:00:00Z",
            "test_case": {
                "name": name,
                "file_path": "tests/test_x.py",
                "quarantined": False,
                "codeowners": ["@owner"],
                "html_url": "https://app.trunk.io/org/flaky-tests/test/abc",
            },
        },
    }


def _run(webhook: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["node", "-e", TRANSFORM.read_text() + HARNESS, json.dumps(webhook)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


# -----------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["FLAKY", "BROKEN"])
def test_a_flaky_or_broken_transition_files_a_labeled_issue(status: str) -> None:
    out = _run(_event(status))
    assert out["cancel"] is False
    issue = out["payload"]
    assert issue["title"] == f"[{status}] tests/test_x.py::test_y"
    assert issue["labels"] == ["ci:flaky-test", "area:test-infra"]
    assert "https://app.trunk.io/org/flaky-tests/test/abc" in issue["body"]
    assert "`tests/test_x.py`" in issue["body"]


def test_a_recovery_is_cancelled_not_sent() -> None:
    out = _run(_event("HEALTHY"))
    assert out["cancel"] is True
    assert "title" not in out["payload"], "a cancelled event must not carry an issue body"


def test_the_title_fits_githubs_limit() -> None:
    out = _run(_event("FLAKY", name="t" * 400))
    assert len(out["payload"]["title"]) == 250
