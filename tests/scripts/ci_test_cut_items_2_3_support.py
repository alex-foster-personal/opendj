"""Shared tree and selector helpers for the TEST-CUT items 2 and 3 test modules (issue #10944)."""

from __future__ import annotations

from pathlib import Path

from scripts.ci_test_selection_rules import (
    Policy,
    select_tests,
)

IGNORES = ["tests/analysis"]
TREE = {
    "apps/__init__.py": "",
    "apps/core/__init__.py": "",
    "apps/core/engine.py": "VALUE = 1\n",
    "apps/core/gone.py": "GONE = 1\n",
    "apps/core/notes.md": "notes\n",
    "apps/other.py": "OTHER = 2\n",
    "tests/scripts/test_engine.py": "from apps.core.engine import VALUE\n",
    "tests/scripts/test_names_engine.py": 'TARGET = "apps/core/engine.py"\n',
    "tests/scripts/test_reads_notes.py": 'NOTES = ROOT / "apps/core/notes.md"\n',
    "tests/scripts/test_imports_gone.py": "from apps.core.gone import GONE\n",
    "tests/scripts/test_workflow.py": 'CI = ROOT / ".github/workflows/ci.yml"\n',
    "tests/scripts/test_all_workflows.py": 'W = sorted((ROOT / ".github" / "workflows").glob("*.yml"))\n',
    "tests/scripts/test_fixture_named.py": 'DATA = FIXTURES / "library.json"\n',
    "tests/scripts/test_always.py": "def test_tree():\n    pass\n",
    "tests/other/test_reads_notes_too.py": 'NOTES = ROOT / "apps/core/notes.md"\n',
    "tests/other/test_other.py": "from apps.other import OTHER\n",
    "tests/scripts/data/golden.csv": "a\n",
    ".github/workflows/ci.yml": "on: push\n",
    ".github/workflows/new.yml": "on: push\n",
    "tests/fixtures/library.json": "{}\n",
    "scripts/__init__.py": "",
    "scripts/ci_test_selection.py": "X = 1\n",
    "scripts/ci_helper.py": "Y = 1\n",
    "ci/pr-always-run.txt": "tests/scripts/test_always.py\n",
}


def build_tree(tmp_path: Path) -> Path:
    for name, body in TREE.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _select(tree: Path, policy: Policy, *changed: str):
    return select_tests(list(changed), IGNORES, tree, policy)


ITEM3 = Policy(ci_config_by_name=True)
DEMOTE_SCRIPTS = Policy(demote_weak_under=("tests/scripts",))
