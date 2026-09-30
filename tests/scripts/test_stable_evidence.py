"""OPS-18 stable-channel evidence schema and release gate.

Fixture files only. No network. A sha is stable only when every evidence key
is present and green for THAT sha; a red-team blocking bug means a new sha.

- if a complete green evidence file is rejected then the gate is unusable
- if missing evidence is refused without naming the item then the operator
  cannot finish the file
- if an open blocking bug still confers stable then a broken build ships
- if nightly requires the same file then nightlies cannot publish
- if just release --channel stable does not call the gate before just dmg
  then a Mac spend happens on a sha that was never eligible
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.stable_evidence import (
    _WORKFLOW_TO_SUITE,
    SUITE_KEYS,
    StableEvidenceError,
    append_red_team,
    append_signing,
    append_suite,
    evidence_path,
    gate_stable,
    load_evidence,
    parse_notary_submission_id,
    stamp_payload_identity,
    validate_evidence,
    write_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _green_suites() -> dict[str, Any]:
    return {key: {"run_id": f"run-{key}", "conclusion": "success"} for key in SUITE_KEYS}


def _green_evidence(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "sha": SHA,
        "suites": _green_suites(),
        "red_team": {
            "pass_id": "pass-1",
            "agents": ["opus-resident", "haiku-1"],
            "sessions": ["s1", "s2"],
            "open_blocking_bug_count": 0,
            "report_path": "ops/redteam/pass-1.md",
        },
        "signing": {
            "identity": "Developer ID Application: Example (ABCD123456)",
            "notarization_submission_id": "2efe2717-52ef-43a5-96dc-0797c3de4eaa",
        },
        "written_by": "scripts.stable_evidence",
        "written_at_utc": "2026-09-12T12:00:00Z",
    }
    body.update(overrides)
    return body


def _write(tmp_path: Path, body: dict[str, Any], sha: str = SHA) -> Path:
    path = evidence_path(tmp_path, sha)
    write_evidence(path, body)
    return path


def test_a_complete_green_file_is_stable(tmp_path: Path) -> None:
    path = _write(tmp_path, _green_evidence())
    loaded = load_evidence(path)
    validate_evidence(loaded, expected_sha=SHA)
    missing = gate_stable(tmp_path, SHA)
    assert missing == []


def test_a_missing_file_names_the_path(tmp_path: Path) -> None:
    missing = gate_stable(tmp_path, SHA)
    assert missing == [f"evidence file {tmp_path / f'{SHA}.json'}"]


def test_schema_rejects_a_short_sha(tmp_path: Path) -> None:
    body = _green_evidence(sha="abc")
    path = tmp_path / "abc.json"
    path.write_text(json.dumps(body) + "\n", encoding="utf-8")
    with pytest.raises(StableEvidenceError, match="full 40-character"):
        validate_evidence(json.loads(path.read_text(encoding="utf-8")), expected_sha="abc")


@pytest.mark.parametrize("key", ["suites", "red_team", "signing", "written_by", "written_at_utc"])
def test_a_missing_top_level_key_is_named(tmp_path: Path, key: str) -> None:
    body = _green_evidence()
    del body[key]
    _write(tmp_path, body)
    missing = gate_stable(tmp_path, SHA)
    assert key in missing
    assert missing != []


@pytest.mark.parametrize("suite", list(SUITE_KEYS))
def test_a_missing_suite_is_named(tmp_path: Path, suite: str) -> None:
    body = _green_evidence()
    del body["suites"][suite]
    _write(tmp_path, body)
    missing = gate_stable(tmp_path, SHA)
    assert f"suites.{suite}" in missing


@pytest.mark.parametrize("suite", list(SUITE_KEYS))
def test_a_failed_suite_is_named_not_green(tmp_path: Path, suite: str) -> None:
    body = _green_evidence()
    body["suites"][suite]["conclusion"] = "failure"
    _write(tmp_path, body)
    missing = gate_stable(tmp_path, SHA)
    assert any(suite in item and "failure" in item for item in missing)


def test_an_open_blocking_bug_refuses_stable_and_demands_a_new_sha(
    tmp_path: Path,
) -> None:
    body = _green_evidence()
    body["red_team"]["open_blocking_bug_count"] = 2
    _write(tmp_path, body)
    missing = gate_stable(tmp_path, SHA)
    joined = " ".join(missing)
    assert "open_blocking_bug_count" in joined
    assert "2" in joined
    assert "new sha" in joined


def test_evidence_for_a_different_sha_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, _green_evidence(sha=OTHER_SHA), sha=SHA)
    missing = gate_stable(tmp_path, SHA)
    assert any("sha" in item and OTHER_SHA in item for item in missing)


def test_append_suite_creates_and_merges(tmp_path: Path) -> None:
    append_suite(
        tmp_path,
        SHA,
        suite="fast_lane",
        run_id="111",
        conclusion="success",
        written_by="ci",
    )
    first = load_evidence(evidence_path(tmp_path, SHA))
    assert first["suites"]["fast_lane"]["run_id"] == "111"
    append_suite(
        tmp_path,
        SHA,
        suite="e2e",
        run_id="222",
        conclusion="success",
        written_by="ci",
    )
    second = load_evidence(evidence_path(tmp_path, SHA))
    assert second["suites"]["fast_lane"]["run_id"] == "111"
    assert second["suites"]["e2e"]["run_id"] == "222"
    assert second["sha"] == SHA


def test_append_red_team_and_signing(tmp_path: Path) -> None:
    append_red_team(
        tmp_path,
        SHA,
        pass_id="p1",
        agents=["a1"],
        sessions=["s1"],
        open_blocking_bug_count=0,
        report_path="report.md",
        written_by="red-team",
    )
    append_signing(
        tmp_path,
        SHA,
        identity="Developer ID Application: Example (ABCD123456)",
        notarization_submission_id="deadbeef-0000-0000-0000-000000000001",
        written_by="just dmg",
    )
    body = load_evidence(evidence_path(tmp_path, SHA))
    assert body["red_team"]["pass_id"] == "p1"
    assert body["signing"]["notarization_submission_id"].startswith("deadbeef")


def test_cli_gate_names_the_missing_item_and_exits_nonzero(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.stable_evidence",
            "gate",
            "--sha",
            SHA,
            "--evidence-dir",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert SHA in result.stderr
    assert "evidence file" in result.stderr


def test_cli_gate_accepts_a_green_fixture(tmp_path: Path) -> None:
    _write(tmp_path, _green_evidence())
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.stable_evidence",
            "gate",
            "--sha",
            SHA,
            "--evidence-dir",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "stable evidence is complete" in result.stderr


def test_parse_notary_submission_id_reads_the_last_id_line() -> None:
    log = (
        "Submission ID received\n"
        "  id: 11111111-1111-1111-1111-111111111111\n"
        "Processing complete\n"
        "  id: 2efe2717-52ef-43a5-96dc-0797c3de4eaa\n"
        "  status: Accepted\n"
    )
    assert parse_notary_submission_id(log) == "2efe2717-52ef-43a5-96dc-0797c3de4eaa"


def test_stamp_payload_identity_writes_channel_and_evidence_time(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"identity": {"git_sha": "abc1234", "lane_label": ""}}),
        encoding="utf-8",
    )
    stamp_payload_identity(
        manifest,
        channel="stable",
        evidence_written_at_utc="2026-09-12T12:00:00Z",
    )
    identity = json.loads(manifest.read_text(encoding="utf-8"))["identity"]
    assert identity["release_channel"] == "stable"
    assert identity["evidence_written_at_utc"] == "2026-09-12T12:00:00Z"


def test_release_publisher_gates_stable_before_building() -> None:
    publisher = (REPO_ROOT / "scripts/release.sh").read_text(encoding="utf-8")
    assert 'CHANNEL="nightly"' in publisher
    assert "--channel" in publisher
    assert "python3 -m scripts.stable_evidence gate" in publisher
    gate_at = publisher.index("python3 -m scripts.stable_evidence gate")
    dmg_at = publisher.index("just dmg")
    darwin_at = publisher.index("just release requires macOS")
    assert gate_at < dmg_at
    assert gate_at < darwin_at
    assert '"channel": os.environ["CHANNEL"]' in publisher


def test_just_release_forwards_channel_args() -> None:
    recipe = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    assert "release *args:" in recipe
    release_block = recipe.split("release *args:", 1)[1].split("\n\n", 1)[0]
    assert "scripts/release.sh" in release_block
    assert "{{args}}" in release_block or "{{ args }}" in release_block


def test_just_dmg_appends_signing_after_notarize() -> None:
    recipe = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    assert "scripts.stable_evidence append-signing" in recipe
    assert recipe.index("scripts/sign_macos_developer_id.sh notarize") < recipe.index(
        "scripts.stable_evidence append-signing"
    )


def test_ci_appends_suite_results_for_every_recorded_workflow() -> None:
    """The scheduled pass records exactly the workflows the suite map names."""
    from scripts.stable_evidence_batch import RECORDED_WORKFLOWS

    assert frozenset(_WORKFLOW_TO_SUITE) == RECORDED_WORKFLOWS
    text = (REPO_ROOT / ".github/workflows/stable-evidence.yml").read_text(encoding="utf-8")
    assert "workflow_run" not in yaml.safe_load(text)[True]
    assert "scripts.stable_evidence_batch" in text
    assert "OPENDJ_STABLE_EVIDENCE_DIR" in text


def test_just_release_stable_names_missing_evidence_without_a_mac() -> None:
    """Acceptance: just release --channel stable names the missing item.

    The Darwin/signing spend must not run first: otherwise Linux (and a Mac
    with no evidence) reports the wrong refusal.
    """
    just_bin = shutil.which("just")
    if just_bin is None:
        pytest.skip("just is not on PATH")
    env = dict(os.environ)
    env["OPENDJ_STABLE_EVIDENCE_DIR"] = "/tmp/opendj-stable-evidence-absent"
    result = subprocess.run(
        [just_bin, "release", "--channel", "stable"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "macOS" not in combined
    assert "evidence file" in combined or "missing" in combined.lower()
