"""SCRIPTS-11334: TEST disposition validates pytest targets by running them.

[if] a skipped pytest TEST target is proposed [then] validation refuses it, [else stop].
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import prompt_disposition as disp
from scripts import prompt_targets as targets

pytestmark = pytest.mark.requirement("SCRIPTS-11334")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DOC = '    """[if] the cited behavior regresses [then] this test fails, [else stop]."""'
_MARKED = 'pytestmark = pytest.mark.requirement("SCRIPTS-11334")'


def _test(name: str, *decorators: str, args: str = "") -> list[str]:
    return [*decorators, f"def {name}({args}):", _DOC, "", ""]


_MARKS = [
    "import sys",
    "",
    "import pytest",
    "",
    _MARKED,
    "",
    "",
    *_test("test_plain"),
    *_test("test_skip", '@pytest.mark.skip(reason="literal skip")'),
    *_test("test_skipif_true", '@pytest.mark.skipif(True, reason="true here")'),
    "class _Base:",
    '    pytestmark = pytest.mark.skip(reason="base class mark")',
    "",
    "",
    "class TestInherits(_Base):",
    *["    " + line for line in _test("test_inherited", args="self")],
]
_MODULES = {
    "tests/test_marks.py": _MARKS,
}


@pytest.fixture(scope="module")
def checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Disposable checkout with synthetic pytest modules and a real reqs.json."""
    root = tmp_path_factory.mktemp("disp-pytest-run") / "checkout"
    for rel, lines in _MODULES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
    shutil.copy(PROJECT_ROOT / "reqs.json", root / "reqs.json")
    return root


def test_skip_marked_pytest_node_is_refused(checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a TEST target is a pytest node under @pytest.mark.skip [then] exit 2 naming skip, [else stop]."""
    target = "tests/test_marks.py::test_skip"
    with pytest.raises(SystemExit) as exc:
        disp._validated_target(checkout, "TEST", target)
    assert exc.value.code == disp.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "skipped test:" in err and target in err and "literal skip" in err


def test_always_true_skipif_pytest_node_is_refused(checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a TEST target's skipif is true on every runner [then] exit 2, [else stop]."""
    target = "tests/test_marks.py::test_skipif_true"
    with pytest.raises(SystemExit) as exc:
        disp._validated_target(checkout, "TEST", target)
    assert exc.value.code == disp.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "skipped test:" in err and target in err and "true here" in err


def test_inherited_suite_skip_pytest_node_is_refused(checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a TEST target is skipped by an inherited suite skip [then] exit 2, [else stop]."""
    target = "tests/test_marks.py::TestInherits::test_inherited"
    with pytest.raises(SystemExit) as exc:
        disp._validated_target(checkout, "TEST", target)
    assert exc.value.code == disp.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "skipped test:" in err and target in err and "base class mark" in err


def test_running_pytest_node_that_passes_validates(checkout: Path) -> None:
    """[if] a TEST target pytest node runs and passes under pytest -p no:randomly -rs [then] ok, [else stop]."""
    target = "tests/test_marks.py::test_plain"
    assert disp._validated_target(checkout, "TEST", target) == target


def test_collect_only_alone_is_insufficient_for_skipped_node(checkout: Path) -> None:
    """[if] collection-only is the only check for a skippable node [then] refuse is wrong, [else stop]."""
    target = "tests/test_marks.py::test_skip"
    assert targets.collect_only_lists_node(checkout, target)
    assert targets._run_pytest_node(checkout, target)[0] == "skipped"
    with pytest.raises(SystemExit):
        disp._validated_target(checkout, "TEST", target)


def test_validation_runs_pytest_not_collect_only(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] validation only used --collect-only [then] skipped nodes could pass, [else stop]."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def record_run(cmd: list[str], **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", record_run)
    target = "tests/test_marks.py::test_plain"
    disp._validated_target(checkout, "TEST", target)
    run_argv = next(c for c in calls if "-m" in c and "pytest" in c and "--collect-only" not in c)
    assert "-rs" in run_argv and "no:randomly" in " ".join(run_argv)
    assert target in run_argv
