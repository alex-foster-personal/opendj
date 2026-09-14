"""Duplicate-writer periodic check (DEVOPS-10, issue #1580).

The instrument enumerates fleet state files declared in a committed
manifest and fails on more than one writer, a reader of a path no writer
produces, or near-duplicate names (dash vs underscore, old vs new prefix).

Acceptance:
- [if] today's known duplicates are presented as live [then] the check is
  red and names seat-b-five-hour-pct, seat-b-five-hour-pct, and
  acct3-5h-pct
- [if] those duplicates are tombstoned with a Supersedes line [then] the
  check is green
- [if] a path has two writers [then] the check names the path and exits 1
- [if] a reader names a path no writer produces [then] the check exits 1
- [if] the committed ops/fleet inventory is clean [then] the check exits 0
- [if] the periodic workflow drops the job or the ledger row [then] this
  fails
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts import duplicate_writer_check as mod

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "periodic-checks.yml"
MODULE = "scripts.duplicate_writer_check"
ROW = "14"
KNOWN_DUPES = REPO_ROOT / "tests" / "fixtures" / "fleet-state" / "known-duplicates-2026-09-09.yaml"
COMMITTED = REPO_ROOT / "ops" / "fleet" / "state-files.yaml"
POLICY = REPO_ROOT / "docs" / "ops" / "periodic-checks.md"
CONVENTION = REPO_ROOT / "docs" / "conventions" / "supersession.md"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _check_job() -> dict:
    for job in (_doc().get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if MODULE in str(step.get("run", "")):
                return job
    raise AssertionError(
        f"no job in {WORKFLOW.name} runs {MODULE}; row {ROW} of "
        "docs/ops/periodic-checks.md is an unscheduled policy row"
    )


def _check_step() -> dict:
    for step in _check_job().get("steps") or []:
        if MODULE in str(step.get("run", "")):
            return step
    raise AssertionError(f"the duplicate-writer job has no step running {MODULE}")


def _report_job() -> dict:
    jobs = _doc()["jobs"]
    assert "report" in jobs, "the ledger-comment job is gone; nothing lands"
    return jobs["report"]


def _report_script() -> str:
    return "\n".join(str(s.get("run", "")) for s in _report_job()["steps"])


@pytest.mark.requirement("DEVOPS-10")
def test_first_run_lists_known_duplicates_and_is_red() -> None:
    """Wed 9 Sep 2026 inventory, before cleanup: the check must go red and
    name every known duplicate. This is the first-run evidence; cleanup is
    a later assertion against the committed manifest.

    [if] the known-duplicates fixture runs [then] it goes red naming all three dupes, [else stop].
    """
    report = mod.evaluate(_load(KNOWN_DUPES), scan_dir=None)
    text = mod.render(report)
    assert report.findings, "the Wed 9 Sep 2026 snapshot must fail the check"
    assert report.exit_code == mod.EXIT_FINDINGS
    for name in (
        "seat-b-five-hour-pct",
        "seat-b-five-hour-pct",
        "acct3-5h-pct",
    ):
        assert name in text, f"first run did not list {name}:\n{text}"
    kinds = {item.kind for item in report.findings}
    assert "near_duplicate" in kinds, text
    assert "multi_writer" in kinds, text
    assert "orphan_reader" in kinds, text


@pytest.mark.requirement("DEVOPS-10")
def test_dash_vs_underscore_is_a_near_duplicate() -> None:
    """[if] a dash-underscore path pair is evaluated [then] it is a near duplicate, [else stop]."""
    payload = {
        "files": [
            {
                "path": "seat-b-five-hour-pct",
                "writer": "a.sh",
                "readers": ["kpi.sh"],
            },
            {
                "path": "seat-b-five-hour-pct",
                "writer": "a.sh",
                "readers": ["kpi.sh"],
            },
        ]
    }
    report = mod.evaluate(payload, scan_dir=None)
    assert any(item.kind == "near_duplicate" for item in report.findings)
    text = mod.render(report)
    assert "dash vs underscore" in text
    assert report.exit_code == mod.EXIT_FINDINGS


@pytest.mark.requirement("DEVOPS-10")
def test_old_vs_new_prefix_is_a_near_duplicate() -> None:
    """[if] two paths differ by an old vs new prefix [then] it is a near duplicate, [else stop]."""
    payload = {
        "files": [
            {
                "path": "acct3-5h-pct",
                "writer": "legacy.sh",
                "readers": ["kpi.sh"],
            },
            {
                "path": "account3-five-hour-pct",
                "writer": "account-meters.sh",
                "readers": ["kpi.sh"],
            },
        ]
    }
    report = mod.evaluate(payload, scan_dir=None)
    assert any(item.kind == "near_duplicate" for item in report.findings)
    text = mod.render(report)
    assert "old vs new prefix" in text
    assert report.exit_code == mod.EXIT_FINDINGS


@pytest.mark.requirement("DEVOPS-10")
def test_two_writers_of_one_path_fail() -> None:
    """[if] one path has two writers [then] it is named a multi-writer finding, [else stop]."""
    payload = {
        "files": [
            {
                "path": "cap-agents",
                "writer": ["watchdog.sh", "spawn-worker.sh"],
                "readers": ["ops/fleet/kpi.sh"],
            }
        ]
    }
    report = mod.evaluate(payload, scan_dir=None)
    assert any(
        item.kind == "multi_writer" and item.path == "cap-agents" for item in report.findings
    )
    assert report.exit_code == mod.EXIT_FINDINGS


@pytest.mark.requirement("DEVOPS-10")
def test_reader_of_a_path_no_writer_produces_fails() -> None:
    """[if] a reader names a path with no writer [then] it is an orphan reader, [else stop]."""
    payload = {
        "files": [
            {
                "path": "ghost-five-hour-pct",
                "writer": "none",
                "readers": ["ops/fleet/kpi.sh"],
            }
        ]
    }
    report = mod.evaluate(payload, scan_dir=None)
    assert any(item.kind == "orphan_reader" for item in report.findings)
    assert report.exit_code == mod.EXIT_FINDINGS


@pytest.mark.requirement("DEVOPS-10")
def test_tombstone_with_supersedes_is_not_a_live_duplicate() -> None:
    """[if] duplicates are tombstoned with supersedes [then] no findings remain, [else stop]."""
    payload = {
        "files": [
            {
                "path": "<account>-five-hour-pct",
                "writer": "~/jobs/account-meters.sh",
                "readers": ["ops/fleet/kpi.sh"],
                "supersedes": ["seat-b-five-hour-pct", "acct3-5h-pct"],
            },
            {
                "path": "seat-b-five-hour-pct",
                "status": "tombstone",
                "replaced_by": "<account>-five-hour-pct",
                "writer": "none",
                "readers": [],
            },
            {
                "path": "acct3-5h-pct",
                "status": "tombstone",
                "replaced_by": "<account>-five-hour-pct",
                "writer": "none",
                "readers": [],
            },
        ]
    }
    report = mod.evaluate(payload, scan_dir=None)
    assert report.findings == ()
    assert report.exit_code == mod.EXIT_OK


@pytest.mark.requirement("DEVOPS-10")
def test_reading_a_tombstone_fails_loud() -> None:
    """[if] a tombstoned path is still read [then] it is a tombstone-read finding, [else stop]."""
    payload = {
        "files": [
            {
                "path": "<account>-five-hour-pct",
                "writer": "~/jobs/account-meters.sh",
                "readers": ["ops/fleet/kpi.sh"],
                "supersedes": ["seat-b-five-hour-pct"],
            },
            {
                "path": "seat-b-five-hour-pct",
                "status": "tombstone",
                "replaced_by": "<account>-five-hour-pct",
                "writer": "none",
                "readers": ["ops/fleet/kpi.sh"],
            },
        ]
    }
    report = mod.evaluate(payload, scan_dir=None)
    assert any(item.kind == "tombstone_read" for item in report.findings)
    assert report.exit_code == mod.EXIT_FINDINGS


@pytest.mark.requirement("DEVOPS-10")
def test_unreadable_manifest_is_unknown_not_a_clean_zero(tmp_path: Path) -> None:
    """[if] the manifest cannot be read [then] the check reports UNKNOWN, not zero, [else stop]."""
    missing = tmp_path / "no-such.yaml"
    code, text = mod.run_check(missing, scan_dir=None)
    assert code == mod.EXIT_UNKNOWN
    assert "UNKNOWN" in text
    assert "findings=0" not in text


@pytest.mark.requirement("DEVOPS-10")
def test_committed_manifest_is_green_after_cleanup() -> None:
    """The inventory that lands on main must be green. The red first-run is
    the fixture above, not this file.

    [if] the committed fleet manifest is evaluated [then] it is green with no dupes, [else stop].
    """
    payload = _load(COMMITTED)
    report = mod.evaluate(payload, scan_dir=REPO_ROOT / "ops" / "fleet")
    assert report.exit_code == mod.EXIT_OK, mod.render(report)
    assert report.findings == ()
    live = [entry["path"] for entry in payload["files"] if entry.get("status") != "tombstone"]
    tombs = [entry["path"] for entry in payload["files"] if entry.get("status") == "tombstone"]
    assert "seat-b-five-hour-pct" in tombs
    assert "acct3-5h-pct" in tombs
    assert "seat-b-five-hour-pct" not in live
    live_entries = [e for e in payload["files"] if e.get("status") != "tombstone"]
    assert any("supersedes" in entry for entry in live_entries)


@pytest.mark.requirement("DEVOPS-10")
def test_second_writer_on_committed_inventory_goes_red() -> None:
    """Mutation: bolting a second writer onto a live path is the defect.

    [if] a second writer is bolted onto a live committed path [then] it goes red, [else stop].
    """
    payload = _load(COMMITTED)
    live = next(entry for entry in payload["files"] if entry.get("status") != "tombstone")
    writer = live["writer"]
    if isinstance(writer, str):
        live["writer"] = [writer, "bolted-beside.sh"]
    else:
        live["writer"] = [*list(writer), "bolted-beside.sh"]
    report = mod.evaluate(payload, scan_dir=None)
    assert any(item.kind == "multi_writer" for item in report.findings)
    assert report.exit_code == mod.EXIT_FINDINGS


@pytest.mark.requirement("DEVOPS-10")
def test_cli_json_names_findings_and_exits_nonzero(tmp_path: Path) -> None:
    """[if] the CLI runs --json on known dupes [then] it exits nonzero naming them, [else stop]."""
    dest = tmp_path / "dupes.yaml"
    dest.write_text(KNOWN_DUPES.read_text(encoding="utf-8"), encoding="utf-8")
    code, text = mod.run_check(dest, scan_dir=None, as_json=True)
    assert code == mod.EXIT_FINDINGS
    payload = json.loads(text)
    assert payload["verdict"] == "FINDINGS"
    names = " ".join(item["path"] for item in payload["findings"])
    assert "seat-b-five-hour-pct" in names
    assert "acct3-5h-pct" in names


@pytest.mark.requirement("DEVOPS-10")
def test_convention_paragraph_exists() -> None:
    """[if] the supersession doc is read [then] it defines Supersedes and tombstone, [else stop]."""
    text = CONVENTION.read_text(encoding="utf-8")
    assert "Supersedes:" in text
    assert "Add beside" in text
    assert "tombstone" in text
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "docs/conventions/supersession.md" in agents


@pytest.mark.requirement("DEVOPS-10")
def test_policy_table_has_row_14() -> None:
    """[if] the policy doc is read [then] row 14 names the duplicate-writer check, [else stop]."""
    text = POLICY.read_text(encoding="utf-8")
    assert "| 14 |" in text
    assert "duplicate-writer" in text or "duplicate writer" in text
    assert MODULE.replace(".", "/") in text or "duplicate_writer_check" in text


@pytest.mark.requirement("DEVOPS-10")
def test_workflow_runs_the_check_inside_the_merge_window() -> None:
    """[if] the check job is inspected [then] it runs only in the merge window, [else stop]."""
    job = _check_job()
    assert job.get("if") == "needs.window.outputs.run == 'true'", (
        f"the job is not gated on the merge window: if={job.get('if')!r}"
    )
    step = _check_step()
    assert MODULE in str(step.get("run", ""))


@pytest.mark.requirement("DEVOPS-10")
def test_the_ledger_comment_carries_the_row() -> None:
    """[if] the ledger comment is inspected [then] row 14 marks that job pass/fail, [else stop]."""
    script = _report_script()
    assert f"| {ROW} |" in script, (
        f"the ledger comment has no row {ROW} line; a failure in it is invisible"
    )
    env = {
        key: value
        for step in _report_job()["steps"]
        for key, value in (step.get("env") or {}).items()
    }
    assert env.get("R_DUPES") == "${{ needs.duplicate-writer.result }}", (
        "the row's pass/fail mark is not taken from the duplicate-writer job: "
        f"R_DUPES={env.get('R_DUPES')!r}"
    )
    assert '$(mark "$R_DUPES")' in script
    assert "duplicate-writer" in (_report_job().get("needs") or []), (
        "the report job does not depend on the duplicate-writer job"
    )
