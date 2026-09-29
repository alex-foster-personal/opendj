"""scan_sast.sh positive control: the file floor is the TRACKED control list, not a walk.

Live failure Wed 16 Sep 2026 on demon-llama: pytest pointed at the two control .py files
wrote tests/fixtures/security/sast-control/__pycache__/*.pyc, a `find` walk counted 6,
semgrep scanned 4, and every run read UNKNOWN blaming the rules.

- if a stray __pycache__/*.pyc and an untracked .txt sit in the control dir then the
  control still passes (floor stays at the tracked count)
- if a control file is unmerged (one index entry per conflict stage) then the control
  still passes (the path counts once)
- if a tracked control file is missing from the working tree then UNKNOWN naming it
- if a tracked control file is one semgrep does not scan then UNKNOWN naming it
- if semgrep-summary --expect-file names a path absent from paths.scanned then UNKNOWN
  naming it, with or without the file floor holding
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.scripts.sast_control_fixtures import (
    CONTROL_DIR,
    ROOT,
    git,
    init_scan_repo,
    tracked_control_files,
)

SEMGREP_BIN = ROOT / ".tmp" / "security" / "bin"
SECSCAN = ROOT / "scripts" / "security" / "secscan.py"

needs_scanners = pytest.mark.skipif(
    not ((SEMGREP_BIN / "semgrep").exists() and (SEMGREP_BIN / "uv").exists()),
    reason="semgrep or uv not installed in .tmp/security/bin",
)


# ----- helpers -----------------------------------------------------------------------------------
def _fixture_repo(tmp_path: Path, extra_tracked: dict[str, str] | None = None) -> Path:
    """The shared scan repo plus any extra tracked files, then a docs-only diff on top.

    The docs-only diff makes `scan_sast.sh pr` run the control and then SKIP.
    """
    repo = init_scan_repo(tmp_path)
    for rel, body in (extra_tracked or {}).items():
        (repo / rel).write_text(body, encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "foo.md").write_text("# one\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    (repo / "docs" / "foo.md").write_text("# two\n", encoding="utf-8")
    git(repo, "commit", "-am", "change")
    return repo


def _run_pr_scan(repo: Path, work: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = work / "bin"
    bin_dir.mkdir(parents=True)
    for tool in ("semgrep", "uv"):
        os.symlink(SEMGREP_BIN / tool, bin_dir / tool)
    env = os.environ.copy()
    env["SECURITY_WORK_DIR"] = str(work)
    env["SECURITY_BASE_SHA"] = git(repo, "rev-parse", "HEAD~1").strip()
    env["SECURITY_HEAD_SHA"] = git(repo, "rev-parse", "HEAD").strip()
    return subprocess.run(
        ["bash", "scripts/security/scan_sast.sh", "pr"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _summary_row(work: Path) -> str:
    return (work / "summary.tsv").read_text(encoding="utf-8")


# ----- scan_sast.sh end to end (real semgrep) -------------------------------------------------
@needs_scanners
def test_stray_untracked_files_do_not_raise_the_control_floor(tmp_path: Path) -> None:
    """[if] a stray .pyc and .txt sit in the control dir [then] control passes, [else stop]."""
    repo = _fixture_repo(tmp_path)
    control = repo / CONTROL_DIR
    (control / "__pycache__").mkdir()
    (control / "__pycache__" / "stray.cpython-311.pyc").write_bytes(b"\x00stray bytecode\x00")
    (control / "stray.txt").write_text("not a control file\n", encoding="utf-8")
    work = tmp_path / "work"
    proc = _run_pr_scan(repo, work)
    summary = _summary_row(work)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "UNKNOWN" not in summary, summary
    assert "\tSKIP\t" in summary, summary
    control_json = json.loads((work / "sast" / "control.json").read_text(encoding="utf-8"))
    assert sorted(control_json["paths"]["scanned"]) == tracked_control_files()


@needs_scanners
def test_missing_tracked_control_file_is_unknown_and_named(tmp_path: Path) -> None:
    """[if] a tracked control file is gone from the working tree [then] UNKNOWN naming it."""
    repo = _fixture_repo(tmp_path)
    missing = f"{CONTROL_DIR}/eval_control.ts"
    (repo / missing).unlink()
    work = tmp_path / "work"
    proc = _run_pr_scan(repo, work)
    summary = _summary_row(work)
    assert proc.returncode == 2, proc.stderr or proc.stdout
    assert f"tracked control file missing from the working tree: {missing}" in proc.stderr
    assert "\tUNKNOWN\t" in summary and missing in summary, summary


@needs_scanners
def test_tracked_control_file_semgrep_did_not_scan_is_named(tmp_path: Path) -> None:
    """[if] semgrep skips a tracked control file [then] UNKNOWN naming it, not the rules."""
    unscannable = f"{CONTROL_DIR}/notes.txt"
    repo = _fixture_repo(tmp_path, {unscannable: "plain text, no semgrep language\n"})
    work = tmp_path / "work"
    proc = _run_pr_scan(repo, work)
    summary = _summary_row(work)
    assert proc.returncode == 2, proc.stderr or proc.stdout
    assert f"control files not scanned: {unscannable}" in proc.stderr
    assert "\tUNKNOWN\t" in summary and unscannable in summary, summary
    assert "rules failed to load" not in summary, summary


@needs_scanners
def test_unmerged_control_file_does_not_raise_the_control_floor(tmp_path: Path) -> None:
    """[if] a control file has unmerged conflict stages [then] control passes, [else stop]."""
    repo = _fixture_repo(tmp_path)
    conflicted = f"{CONTROL_DIR}/subprocess_shell_true.py"
    original = (repo / conflicted).read_text(encoding="utf-8")
    trunk = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    git(repo, "checkout", "-q", "-b", "side")
    (repo / conflicted).write_text(original + "# side edit\n", encoding="utf-8")
    git(repo, "commit", "-qam", "side edit")
    git(repo, "checkout", "-q", trunk)
    (repo / conflicted).write_text(original + "# trunk edit\n", encoding="utf-8")
    git(repo, "commit", "-qam", "trunk edit")
    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "side"], capture_output=True, text=True, check=False
    )
    assert merge.returncode != 0, merge.stdout
    # Parseable source in the working tree; the index keeps all three conflict stages.
    git(repo, "checkout", "--ours", "--", conflicted)
    unmerged = git(repo, "ls-files", "-u", "--", conflicted).splitlines()
    assert len(unmerged) == 3, unmerged
    work = tmp_path / "work"
    proc = _run_pr_scan(repo, work)
    summary = _summary_row(work)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "UNKNOWN" not in summary, summary
    control_txt = (work / "sast" / "control.txt").read_text(encoding="utf-8")
    assert f"scanned {len(tracked_control_files())} files" in control_txt, control_txt


# ----- secscan semgrep-summary --expect-file ---------------------------------------------------
EXPECTED = [f"{CONTROL_DIR}/{name}" for name in ("a.py", "b.py", "c.ts", "d.rs")]


def _summary(tmp_path: Path, scanned: list[str], min_files: int) -> tuple[int, str]:
    scan = tmp_path / "control.json"
    doc = {
        "results": [],
        "errors": [],
        "time": {"rules": [{"id": "r0"}]},
        "paths": {"scanned": scanned},
    }
    scan.write_text(json.dumps(doc), encoding="utf-8")
    expect_args = [arg for path in EXPECTED for arg in ("--expect-file", path)]
    proc = subprocess.run(
        [
            sys.executable,
            str(SECSCAN),
            "semgrep-summary",
            str(scan),
            "--min-rules",
            "1",
            "--min-files",
            str(min_files),
            *expect_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stderr


def test_summary_names_control_file_below_the_floor(tmp_path: Path) -> None:
    """[if] 3 of 4 expected files scanned [then] UNKNOWN naming the missing one, [else stop]."""
    rc, stderr = _summary(tmp_path, EXPECTED[:3], min_files=4)
    assert rc == 2
    assert f"fewer than the 4 floor; control files not scanned: {EXPECTED[3]}" in stderr, stderr


def test_summary_names_unscanned_file_even_when_the_floor_holds(tmp_path: Path) -> None:
    """[if] 4 scanned but one is not expected [then] UNKNOWN naming the expected one."""
    rc, stderr = _summary(tmp_path, [*EXPECTED[:3], f"{CONTROL_DIR}/stray.py"], min_files=4)
    assert rc == 2
    assert f"control files not scanned: {EXPECTED[3]}" in stderr, stderr


def test_summary_passes_when_every_expected_file_was_scanned(tmp_path: Path) -> None:
    """[if] every expected file is in paths.scanned [then] exit 0: the flag can say yes."""
    rc, stderr = _summary(tmp_path, list(EXPECTED), min_files=4)
    assert rc == 0, stderr
