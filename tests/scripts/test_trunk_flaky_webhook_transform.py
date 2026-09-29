"""The Trunk Flaky Tests -> GitHub Issues webhook transformation (`ci/trunk/*.transform.js`).

Svix runs the file's `handler(webhook)`; these tests run the same file under node. The event
payload is Trunk's own published example for `v2.test_case.status_changed`, copied verbatim
and checksum-pinned in `tests/fixtures/trunk_webhook_v2_test_case_status_changed.json`, whose
`_provenance` block records the catalog URL and fetch time. It is not a live delivery from
this repo's endpoint: none has been captured yet. The Svix envelope (`eventType`, `method`,
`url`, `payload`, `cancel`) is the one Trunk's GitHub Issues guide documents
(https://docs.trunk.io/flaky-tests/webhooks/github-issues-integration).

Regression lines:
  - if any byte of the fixture (example or schema lists) changes then the oracle is unproven
  - if the transform reads a field the schema does not require then a live event can crash it
  - if a missing or mistyped required field still files an issue then drift hides behind defaults
  - if an allowed empty value renders as "unknown" or "none" then empty reads as missing
  - if an allowed empty value is rejected then Trunk's own published example is dropped
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

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSFORM = REPO_ROOT / "ci" / "trunk" / "flaky-tests-github-issues.transform.js"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "trunk_webhook_v2_test_case_status_changed.json"
# sha256 of the whole fixture file: the example AND the schema lists the tests use as the oracle.
# Recompute only after re-fetching from the catalog URL in its `_provenance` block.
FIXTURE_FILE_SHA256 = "b427c8ab6928f36312cf64fee807a98fd31993fee153927b8d43597c4d5fbfb3"
FIXTURE = json.loads(FIXTURE_PATH.read_text())
EXAMPLE: dict[str, Any] = FIXTURE["example"]
HARNESS = "\nprocess.stdout.write(JSON.stringify(handler(JSON.parse(process.argv[1]))));\n"


# -----------------------------------------------------------------------------
_ABSENT: Any = object()


def _event(new_status: str | None = None, **test_case: Any) -> dict[str, Any]:
    """Trunk's published example, changing only the fields a test is about. A test_case value
    of _ABSENT deletes that key."""
    payload = copy.deepcopy(EXAMPLE)
    if new_status is not None:
        payload["new_status"] = new_status
    for key, value in test_case.items():
        if value is _ABSENT:
            del payload["test_case"][key]
            continue
        payload["test_case"][key] = value
    return {
        "eventType": payload["type"],
        "method": "POST",
        "url": "https://api.github.com/repos/owner/repo/issues",
        "cancel": False,
        "payload": payload,
    }


def _type_table_keys(source: str, table: str) -> set[str]:
    match = re.search(rf"const {table} = \{{([^}}]*)\}}", source)
    assert match is not None, f"{table} is missing from the transform"
    return set(re.findall(r"(\w+):", match[1]))


def _run(webhook: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["node", "-e", TRANSFORM.read_text() + HARNESS, json.dumps(webhook)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


# -----------------------------------------------------------------------------
def test_the_whole_fixture_file_is_pinned() -> None:
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_FILE_SHA256


def test_the_example_matches_the_digest_of_the_catalog_example() -> None:
    """Kept beside the whole-file pin: this digest is of the example alone, so a re-fetch from
    the catalog can be compared with it without reconstructing this repo's wrapper."""
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


def test_every_field_the_transform_reads_is_type_checked_first() -> None:
    source = TRANSFORM.read_text()
    checked = {
        table: _type_table_keys(source, table) for table in ("PAYLOAD_TYPES", "TEST_CASE_TYPES")
    }
    assert set(re.findall(r"\bp\.(\w+)", source)) <= checked["PAYLOAD_TYPES"]
    assert set(re.findall(r"\bt\.(\w+)", source)) <= checked["TEST_CASE_TYPES"]
    assert checked["PAYLOAD_TYPES"] <= set(FIXTURE["required"])
    assert checked["TEST_CASE_TYPES"] <= set(FIXTURE["test_case_required"])


def test_the_published_flaky_example_files_a_labeled_issue() -> None:
    out = _run(_event())
    assert out["cancel"] is False
    issue = out["payload"]
    assert issue["title"] == "[FLAKY] AlwaysSunnyTest.TestWeather_Beijing"
    assert issue["labels"] == ["ci:flaky-test", "area:test-infra"]
    assert EXAMPLE["test_case"]["html_url"] in issue["body"]
    assert "(was HEALTHY)" in issue["body"]
    assert "- File: (empty)" in issue["body"], "the example's empty file_path"


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


@pytest.mark.parametrize("field", ["name", "file_path", "quarantined", "codeowners", "html_url"])
def test_a_missing_required_field_cancels_with_an_error(field: str) -> None:
    out = _run(_event(**{field: _ABSENT}))
    assert out["cancel"] is True
    assert "title" not in out["payload"], "no issue may be filed with invented values"
    assert f"test_case.{field}" in out["payload"]["error"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quarantined", "false"),
        ("quarantined", None),
        ("codeowners", "@owner"),
        ("codeowners", [7]),
        ("file_path", None),
        ("name", 5),
    ],
)
def test_a_wrongly_typed_required_field_cancels_with_an_error(field: str, value: Any) -> None:
    out = _run(_event(**{field: value}))
    assert out["cancel"] is True
    assert "title" not in out["payload"]
    assert f"test_case.{field}" in out["payload"]["error"]


def test_a_missing_top_level_field_cancels_with_an_error() -> None:
    webhook = _event()
    del webhook["payload"]["timestamp"]
    out = _run(webhook)
    assert out["cancel"] is True
    assert "timestamp" in out["payload"]["error"]


def test_allowed_empty_values_render_as_empty_not_invented() -> None:
    """Trunk's published example itself carries file_path "" and codeowners []."""
    assert EXAMPLE["test_case"]["file_path"] == "" and EXAMPLE["test_case"]["codeowners"] == []
    body = _run(_event())["payload"]["body"]
    assert "- File: (empty)" in body
    assert "- Code owners: (empty)" in body
    assert "unknown" not in body
    assert "- Code owners: none" not in body


def test_present_values_render_verbatim() -> None:
    body = _run(_event(file_path="tests/test_x.py", codeowners=["@a", "@b"]))["payload"]["body"]
    assert "- File: `tests/test_x.py`" in body
    assert "- Code owners: @a, @b" in body
