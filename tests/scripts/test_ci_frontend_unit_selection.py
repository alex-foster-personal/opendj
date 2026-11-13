"""scripts/ci_frontend_unit_selection.py: the frontend unit tests a pull request reaches.

Regression lines:
  - [if] a transitive importer's test is not selected for a leaf change [then] fail, [else stop].
  - [if] a test naming a small src/ directory is not selected for a change under it [then] fail, [else stop].
  - [if] a changed test file is not selected [then] fail, [else stop].
  - [if] a manifest change does not run everything [then] fail, [else stop].
  - [if] an unrelated test is selected [then] fail, [else stop].

-Claude
"""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.ci_frontend_unit_selection as selection
from scripts.ci_frontend_unit_selection import FRONTEND, select

pytestmark = pytest.mark.requirement("DEVOPS-27")


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / FRONTEND
    files = {
        "src/lib/a.ts": "import { b } from './b';\nexport const a = b;\n",
        "src/lib/b.ts": "export const b = 1;\n",
        "src/lib/other.ts": "export const o = 1;\n",
        "src/routes/admin/x.ts": "export const x = 1;\n",
        "tests/unit/a.test.mjs": "await loadTypeScriptModule('src/lib/a.ts');\n",
        "tests/unit/admin.test.mjs": "const d = '../../src/routes/admin';\n",
        "tests/unit/other.test.mjs": "await loadTypeScriptModule('$lib/other.ts');\n",
        "tests/unit/filler1.test.mjs": "\n",
        "tests/unit/filler2.test.mjs": "\n",
        "tests/unit/filler3.test.mjs": "\n",
        "package.json": "{}\n",
        "tests/unit/guard.test.mjs": "scan everything\n",
        "tests/unit/daemon.test.mjs": "import { startRealDaemon } from './fixtures/real-daemon.mjs';\n",
    }
    (tmp_path / "ci").mkdir(exist_ok=True)
    (tmp_path / "ci/frontend-unit-always-run.txt").write_text("# guards\ntests/unit/guard.test.mjs\n")
    (tmp_path / "ci/frontend-unit-links.txt").write_text(
        "# links\nsrc/routes/admin/*.txt -> tests/unit/filler1.test.mjs\n"
    )
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_a_leaf_change_selects_its_transitive_tests_only(tmp_path: Path) -> None:
    result = select(_tree(tmp_path), [f"{FRONTEND}/src/lib/b.ts"])
    assert (result.mode, result.tests) == ("selected", ("tests/unit/a.test.mjs", "tests/unit/guard.test.mjs"))


def test_a_directory_literal_selects_its_test(tmp_path: Path) -> None:
    result = select(_tree(tmp_path), [f"{FRONTEND}/src/routes/admin/x.ts"])
    assert result.tests == ("tests/unit/admin.test.mjs", "tests/unit/guard.test.mjs")


def test_a_changed_test_selects_itself(tmp_path: Path) -> None:
    assert select(_tree(tmp_path), [f"{FRONTEND}/tests/unit/other.test.mjs"]).tests == (
        "tests/unit/guard.test.mjs",
        "tests/unit/other.test.mjs",
    )


def test_a_manifest_change_runs_everything(tmp_path: Path) -> None:
    assert select(_tree(tmp_path), [f"{FRONTEND}/package.json"]).mode == "all"


def test_a_backend_change_selects_the_daemon_tests_and_the_guards(tmp_path: Path) -> None:
    """[if] a .py change skips a test that starts the real daemon [then] fail, [else stop]."""
    assert select(_tree(tmp_path), ["apps/x.py"]).tests == ("tests/unit/daemon.test.mjs", "tests/unit/guard.test.mjs")


def test_an_unreached_non_backend_change_runs_only_the_guards(tmp_path: Path) -> None:
    """[if] a guard is missing from a narrowed run [then] fail, [else stop]."""
    assert select(_tree(tmp_path), ["docs/a.md"]).tests == ("tests/unit/guard.test.mjs",)


def test_a_missing_always_run_test_fails_loud(tmp_path: Path) -> None:
    """[if] a listed guard that does not exist is ignored silently [then] fail, [else stop]."""
    root = _tree(tmp_path)
    (tmp_path / "ci/frontend-unit-always-run.txt").write_text("tests/unit/gone.test.mjs\n")
    with pytest.raises(FileNotFoundError):
        select(root, ["docs/a.md"])


def test_a_hub_selects_its_direct_tests_and_a_capped_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a hub change selects its whole reach [then] fail, [else stop]."""
    root = tmp_path / FRONTEND
    (root / "src/lib").mkdir(parents=True)
    (root / "tests/unit").mkdir(parents=True)
    (root / "package.json").write_text("{}\n")
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci/frontend-unit-always-run.txt").write_text("")
    (tmp_path / "ci/frontend-unit-links.txt").write_text("")
    (root / "src/lib/hub.ts").write_text("export const h = 1;\n")
    for i in range(6):
        (root / f"src/lib/m{i}.ts").write_text("import { h } from './hub';\n")
        (root / f"tests/unit/m{i}.test.mjs").write_text(f"await load('src/lib/m{i}.ts');\n")
    (root / "tests/unit/hub.test.mjs").write_text("await load('src/lib/hub.ts');\n")
    for i in range(20):
        (root / f"tests/unit/filler{i}.test.mjs").write_text("\n")
    monkeypatch.setattr(selection, "HUB_SOURCE_READERS", 3)
    monkeypatch.setattr(selection, "HUB_SAMPLE", 2)
    result = select(root, [f"{FRONTEND}/src/lib/hub.ts"])
    assert "tests/unit/hub.test.mjs" in result.tests
    assert len(result.tests) == 3


def test_a_linked_glob_selects_its_computed_path_test(tmp_path: Path) -> None:
    """[if] a change matching a link glob skips the linked test [then] fail, [else stop]."""
    result = select(_tree(tmp_path), [f"{FRONTEND}/src/routes/admin/notes.txt"])
    assert "tests/unit/filler1.test.mjs" in result.tests
