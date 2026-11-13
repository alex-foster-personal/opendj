"""ci_tree_hygiene launches git by resolved absolute path (AGENTS.md portable process rule).

[if] tree hygiene lists or queries git [then] argv[0] is a resolved absolute git path, [else stop].
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import ci_path_resolve, ci_tree_hygiene
from scripts.affected_tests import REPO
from scripts.ci_tree_hygiene import _git_exe, _git_lines

pytestmark = pytest.mark.requirement("CIPAR-12807")


@pytest.mark.requirement("CIPAR-12807")
def test_git_exe_missing_from_path_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] git is not on PATH [then] tree hygiene raises RuntimeError with the exact message, [else stop]."""
    _git_exe.cache_clear()
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        with pytest.raises(RuntimeError, match=r"^git is not on PATH: cannot list the tracked tree$"):
            _git_exe()
    finally:
        _git_exe.cache_clear()


@pytest.mark.requirement("CIPAR-12807")
def test_git_lines_subprocess_receives_resolved_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] tree hygiene runs git [then] argv[0] is an absolute path named git, [else stop]."""
    seen: list[list[str]] = []

    def record(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, f"{REPO}\n", "")

    monkeypatch.setattr(ci_tree_hygiene.subprocess, "run", record)
    _git_lines(REPO, "rev-parse", "--show-toplevel")
    assert len(seen) == 1
    program = Path(seen[0][0])
    assert program.is_absolute(), seen
    assert program.name.lower().startswith("git"), seen
    assert seen[0][1:4] == ["-C", str(REPO), "rev-parse"]


@pytest.mark.requirement("CIPAR-12807")
def test_git_exe_resolves_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] tree hygiene calls git twice [then] PATH lookup runs once, [else stop]."""
    real_which = shutil.which
    count = 0

    def counting_which(name: str) -> str | None:
        nonlocal count
        if name == "git":
            count += 1
        return real_which(name)

    monkeypatch.setattr(ci_path_resolve.shutil, "which", counting_which)
    _git_exe.cache_clear()
    try:
        _git_lines(REPO, "rev-parse", "--show-toplevel")
        _git_lines(REPO, "rev-parse", "--show-toplevel")
        assert count == 1
    finally:
        _git_exe.cache_clear()
