"""The Trunk Flaky Tests -> GitHub Issues webhook transformation (`ci/trunk/*.transform.js`).

Svix runs the file's `handler(webhook)`; these tests run the same file under node. The event
payload is Trunk's own published example for `v2.test_case.status_changed`, copied verbatim
and checksum-pinned in `tests/fixtures/trunk_webhook_v2_test_case_status_changed.json`, whose
`_provenance` block records the catalog URL and fetch time. It is not a live delivery from
this repo's endpoint: none has been captured yet. The Svix envelope (`eventType`, `method`,
`url`, `payload`, `cancel`) is the one Trunk's GitHub Issues guide documents
(https://docs.trunk.io/flaky-tests/webhooks/github-issues-integration).

Regression lines:
  - if the fixture drifts from Trunk's published example then the tests stop proving the real shape
  - if the transform reads a field the schema does not require then a live event can crash it
  - if a FLAKY transition is cancelled then new flaky tests never reach the issue tracker
  - if a BROKEN transition is cancelled then consistently failing tests are never ticketed
  - if a HEALTHY transition is sent then every recovery files a fresh, bogus issue
  - if the title can exceed 250 characters then GitHub rejects the create with a 422
  - if the issue body drops the Trunk URL then a reader cannot reach the test's history
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSFORM = REPO_ROOT / "ci" / "trunk" / "flaky-tests-github-issues.transform.js"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "trunk_webhook_v2_test_case_status_changed.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text())
EXAMPLE: dict[str, Any] = FIXTURE["example"]
HARNESS = "\nprocess.stdout.write(JSON.stringify(handler(JSON.parse(process.argv[1]))));\n"


# -----------------------------------------------------------------------------
def _event(new_status: str | None = None, name: str | None = None) -> dict[str, Any]:
    """Trunk's published example, changing only the one field a test is about."""
    payload = copy.deepcopy(EXAMPLE)
    if new_status is not None:
        payload["new_status"] = new_status
    if name is not None:
        payload["test_case"]["name"] = name
    return {
        "eventType": payload["type"],
        "method": "POST",
        "url": "https://api.github.com/repos/owner/repo/issues",
        "cancel": False,
        "payload": payload,
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
def test_the_fixture_is_trunks_published_example_unmodified() -> None:
    canon = json.dumps(EXAMPLE, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canon).hexdigest() == FIXTURE["_provenance"]["example_sha256"]
    assert EXAMPLE["type"] == "v2.test_case.status_changed"


def test_the_transform_reads_only_fields_the_schema_requires() -> None:
    source = TRANSFORM.read_text()
    payload_fields = set(re.findall(r"\bp\.(\w+)", source))
    test_case_fields = set(re.findall(r"\bt\.(\w+)", source))
    assert {"new_status", "test_case"} <= payload_fields, "positive control: reads were found"
    assert "html_url" in test_case_fields, "positive control: test_case reads were found"
    assert payload_fields <= set(FIXTURE["required"])
    assert test_case_fields <= set(FIXTURE["test_case_required"])


def test_the_published_flaky_example_files_a_labeled_issue() -> None:
    out = _run(_event())
    assert out["cancel"] is False
    issue = out["payload"]
    assert issue["title"] == "[FLAKY] AlwaysSunnyTest.TestWeather_Beijing"
    assert issue["labels"] == ["ci:flaky-test", "area:test-infra"]
    assert EXAMPLE["test_case"]["html_url"] in issue["body"]
    assert "(was HEALTHY)" in issue["body"]
    assert "- File: `unknown`" in issue["body"], "the example's empty file_path"


def test_a_broken_transition_files_an_issue() -> None:
    out = _run(_event("BROKEN"))
    assert out["cancel"] is False
    assert out["payload"]["title"].startswith("[BROKEN] ")


def test_a_recovery_is_cancelled_not_sent() -> None:
    out = _run(_event("HEALTHY"))
    assert out["cancel"] is True
    assert "title" not in out["payload"], "a cancelled event must not carry an issue body"


def test_the_title_fits_githubs_limit() -> None:
    out = _run(_event(name="t" * 400))
    assert len(out["payload"]["title"]) == 250
