"""Shared git checkouts, payloads and budgets for the ci_test_selection test modules (issue #10971)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

from scripts import ci_test_selection as selection
from scripts.ci_test_budget import BudgetConfig
from scripts.ci_test_selection import (
    Event,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ci_test_selection"
IGNORES = ["tests/analysis"]
REPO = "private_owner/music-dj-tools"

BASE_TREE = {
    "apps/__init__.py": "",
    "apps/core/__init__.py": "",
    "apps/core/engine.py": "VALUE = 1\n",
    "apps/core/rel.py": "from . import engine\n",
    "apps/other.py": "OTHER = 2\n",
    "apps/rootdep.py": "ROOT = 3\n",
    "tests/conftest.py": "from apps.rootdep import ROOT\n",
    "tests/core/test_engine.py": "from apps.core.engine import VALUE\n",
    "tests/core/test_rel.py": "import apps.core.rel\n",
    "tests/other/test_other.py": "from apps.other import OTHER\n",
    "tests/analysis/test_ignored.py": "from apps.core.engine import VALUE\n",
    "README.md": "readme\n",
    "ci/pr-always-run.txt": "# no always-run guards in this fixture\n",
    ".test_durations": json.dumps(
        {
            "tests/core/test_engine.py::test_a": 40.0,
            "tests/core/test_rel.py::test_b": 5.0,
            "tests/other/test_other.py::test_c": 3.0,
        }
    ),
}


def _payload(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def _write(root: Path, files: Mapping[str, str | None]) -> None:
    for name, body in files.items():
        path = root / name
        if body is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _pr_checkout(root: Path, change: dict[str, str | None], base: dict | None = None) -> str:
    """A real pull_request checkout: HEAD is main merged with the PR, HEAD^2 the PR head.

    main moves on after the PR branches, so the merge base is not HEAD^1, which is what
    makes the diff against the merge base (not against main) observable.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "ci@example.com")
    _git(root, "config", "user.name", "ci")
    _write(root, base if base is not None else BASE_TREE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "pr")
    _write(root, change)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "the PR")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    _write(root, {"apps/main_only.py": "MAIN = 1\n"})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "main moves on")
    _git(root, "merge", "-q", "--no-ff", "--no-edit", "pr")
    return head


def _pr_event(head_sha: str) -> Event:
    return Event("pull_request", "af--feature", head_sha, (), 1)


HARD_CAP = BudgetConfig(
    minutes={"pr_head": 3.15, "queue_draft": 3.15},
    overhead_min_per_shard=0.52, min_per_ledger_minute=0.8, shards=5,
    drop_order=("coupling `",), cap="hard",
)  # fmt: skip
# Five shards cost 2.6; 3.15 leaves 0.55 runner-min = 41 ledger-seconds: test_engine (40 s) fits.


def _env(monkeypatch, tmp_path: Path, event_name: str, payload: str, switch: str | None) -> Path:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(FIXTURES / payload))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    if switch is None:
        monkeypatch.delenv("CI_PR_TEST_SELECTION", raising=False)
    else:
        monkeypatch.setenv("CI_PR_TEST_SELECTION", switch)
    # The TEST-CUT variables reach this suite's own CI step, so a test that does not ask
    # for them must not inherit them.
    monkeypatch.delenv(selection.BY_NAME_VARIABLE, raising=False)
    monkeypatch.delenv(selection.DEMOTE_VARIABLE, raising=False)
    monkeypatch.delenv(selection.MAIN_SWITCH_VARIABLE, raising=False)
    monkeypatch.delenv(selection.MAIN_RANGE_VARIABLE, raising=False)
    return summary
