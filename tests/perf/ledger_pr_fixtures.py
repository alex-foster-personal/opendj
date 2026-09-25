"""Shared real-git fixtures for the perf KPI ledger PR tests.

A real upstream repo plus a clone of it, and schema-valid ledger rows, used by
`test_perf_kpi_ledger_pr_worktree.py`, `test_perf_kpi_ledger_pr_recovery.py` and
`test_perf_kpi_ledger_local.py`. Nothing
here fakes git: every helper runs the real `git` binary against a temp directory.

-Claude
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.perf import perf_kpi_ledger_pr
from tests.perf.gh_pr_list_replay import GhPrListReplay, install_pr_list_replay

#: The committed content _init_upstream_and_clone always seeds, i.e. what a
#: fresh clone's ledger looks like before anything in this suite runs -- the
#: `pre_run_content` every test below passes to `update_ledger_pr`, since
#: none of them simulate a genuinely pre-existing dirty edit (that scenario
#: has its own direct test on `_restore_tracked_ledger`).
EMPTY_LEDGER = json.dumps({"schema_version": 2, "entries": []}) + "\n"


def ledger_entry(name: str) -> dict[str, Any]:
    """A schema-valid, already-normalized ledger row identified by ``name``.

    `append_entries` validates by default now (Sol, PR #3827, P1/BLOCKING,
    review 4107678137, "Validate ledger entries before committing them"): a
    bare ``{"name": ...}`` literal is missing every field `validate_entry`
    requires. Every field `validate_entry` would otherwise ADD (`value`,
    `measured`) is included here up front too, so validation is a true
    no-op and entries still compare equal across a round trip through
    `append_entries` -- several assertions below depend on that.
    """
    return {
        "date": "2026-09-01",
        "round": 1,
        "kpi": "test",
        "unit": "ms",
        "machine": "test",
        "source": "test",
        "note": name,
        "value": 1.0,
        "measured": True,
        "name": name,
    }


def run_git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def init_upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A real upstream repo plus a clone of it, standing in for origin/REPO_ROOT."""
    upstream = tmp_path / "upstream.git-checkout"
    upstream.mkdir()
    run_git("init", "-q", "-b", "main", cwd=upstream)
    run_git("config", "user.email", "test@example.com", cwd=upstream)
    run_git("config", "user.name", "Test", cwd=upstream)
    ledger_dir = upstream / "docs" / "perf"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "kpi-ledger.json").write_text(
        json.dumps({"schema_version": 2, "entries": []}) + "\n", encoding="utf-8"
    )
    run_git("add", "-A", cwd=upstream)
    run_git("commit", "-q", "-m", "initial", cwd=upstream)

    repo_root = tmp_path / "repo-root"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(repo_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    run_git("config", "user.email", "test@example.com", cwd=repo_root)
    run_git("config", "user.name", "Test", cwd=repo_root)
    # Real pushes need somewhere to land: make upstream non-bare push-able.
    run_git("config", "receive.denyCurrentBranch", "updateInstead", cwd=upstream)
    return upstream, repo_root


def install_gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, open_pr: bool) -> GhPrListReplay:
    """A REAL `gh` executable on PATH, answering from real captured exchanges.

    Replaces the earlier Python-level `subprocess.run` monkeypatch of `gh`
    (Codex, PR #3827, P1/BLOCKING, review comment 4106092917: "Replace the
    simulated GitHub CLI results"): nothing under `scripts/` is patched here.
    `perf_kpi_ledger_pr`'s own `subprocess.run` genuinely forks and execs whatever
    `gh` resolves to on PATH; see `tests/perf/gh_pr_list_replay.py` for what
    answers it and why that is a real exchange rather than a fabricated one.
    """
    return install_pr_list_replay(
        monkeypatch,
        tmp_path,
        repository=perf_kpi_ledger_pr.REPOSITORY,
        branch=perf_kpi_ledger_pr.LEDGER_PR_BRANCH,
        scenario="pr_open" if open_pr else "no_pr_open",
    )
