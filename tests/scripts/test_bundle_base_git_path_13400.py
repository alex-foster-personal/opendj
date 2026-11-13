"""Bundle baseline git-path resolution regressions (issue #13400, spec e0d4018e section 3d).

- [if] git resolution or a failing git call is not surfaced as BundleBaseError [then] CI measures a wrong baseline, [else stop].
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.ci_bundle_base import BundleBaseError, _git_exe, resolve_merge_parent


@pytest.fixture(autouse=True)
def _fresh_git_cache() -> Iterator[None]:
    _git_exe.cache_clear()
    yield
    _git_exe.cache_clear()


def test_git_on_path_resolves_to_the_same_executable_as_which() -> None:
    """[if] git is on PATH [then] _git_exe returns its resolved absolute path, [else stop]."""
    expected = shutil.which("git")
    assert expected is not None, "fixture host needs git on PATH"
    resolved = _git_exe()
    assert Path(resolved).is_absolute()
    assert Path(resolved) == Path(expected)


def test_path_without_git_raises_the_named_diagnostic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] PATH contains no git executable [then] resolve_merge_parent raises the named git-missing error, [else stop]."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(BundleBaseError, match=r"^git is not on PATH: cannot resolve the bundle baseline$"):
        resolve_merge_parent(tmp_path)


def test_git_failure_while_resolving_baseline_raises(tmp_path: Path) -> None:
    """[if] git exits non-zero while resolving the baseline [then] BundleBaseError names the failed git call, [else stop]."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with pytest.raises(BundleBaseError, match=r"^git rev-parse --short HEAD failed: "):
        resolve_merge_parent(not_a_repo)
