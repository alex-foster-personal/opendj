"""Shared real-git fixtures for the perf KPI ledger PR tests.

A real upstream repo plus a clone of it, and schema-valid ledger rows, used by
`test_perf_kpi_ledger_pr_worktree.py`, `test_perf_kpi_ledger_pr_recovery.py` and
`test_perf_kpi_ledger_local.py`. Nothing
here fakes git: every helper runs the real `git` binary against a temp directory.

Supersedes: nothing on main; these helpers were private to
`test_perf_kpi_ledger_pr_worktree.py` within PR #3827 and are deleted there.

-Claude
"""

from __future__ import annotations

import json
import subprocess
import sys
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


_CONCURRENT_PUSH_HOOK = """#!{python}
import json, os, subprocess, sys
from pathlib import Path

if {hook!r} == "reference-transaction":
    sys.stdin.read()
    if sys.argv[1] != "committed":
        sys.exit(0)
air = Path({air_clone!r})
if air.exists():
    sys.exit(0)
env = {{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}}

def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

git("clone", "-q", {upstream!r}, str(air))
git("config", "user.email", "air@example.com", cwd=air)
git("config", "user.name", "Air", cwd=air)
git("checkout", "-q", "-B", {branch!r}, {start!r}, cwd=air)
doc = {{"schema_version": 2, "entries": json.loads({entries!r})}}
(air / "docs" / "perf" / "kpi-ledger.json").write_text(json.dumps(doc) + "\\n", encoding="utf-8")
git("commit", "-aq", "-m", "air's concurrent push", cwd=air)
git("push", "-q", "origin", {branch!r}, cwd=air)
"""


def install_concurrent_push_hook(
    repo_root: Path,
    hook: str,
    *,
    upstream: Path,
    air_clone: Path,
    start: str,
    entries: list[dict[str, Any]],
) -> None:
    """A real git hook in ``repo_root`` (shared by its worktrees) that, the
    first time git runs it, pushes ``entries`` to the ledger branch from a
    separate clone at ``air_clone``, as another host would, and then lets
    the triggering git operation carry on.

    ``hook`` picks the window: ``post-checkout`` fires on the publish's
    ``git worktree add``, after its lease was taken; ``reference-transaction``
    fires on its first committed ref update, the ``git fetch origin main``
    between ``gh pr list`` and the branch fetch. ``air_clone`` existing
    afterwards is the proof the hook ran. Replaces a monkeypatch of
    ``subprocess.run`` (AGENTS.md: no monkeypatching in tests)."""
    path = repo_root / ".git" / "hooks" / hook
    path.write_text(
        _CONCURRENT_PUSH_HOOK.format(
            python=sys.executable,
            hook=hook,
            air_clone=str(air_clone),
            upstream=str(upstream),
            branch=perf_kpi_ledger_pr.LEDGER_PR_BRANCH,
            start=start,
            entries=json.dumps(entries),
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
