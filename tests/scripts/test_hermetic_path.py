"""Unit coverage for tests/scripts/_hermetic_path.py.

The class of bug this guards: PR #2624 found that
`tests/scripts/test_dmg_preflight_oauth.py` hid doppler by dropping its
WHOLE PATH directory, which also dropped bash on a runner where the two
shared a directory -- the test's pass/fail then depended on the runner's own
layout, not on the code under test. c115babb4 fixed that one instance;
`_path_without`-shaped helpers in test_ci_runner_preflight.py,
test_dmg_preflight.py and test_dmg_sccache.py had the same shape, fixed here
by switching them all onto `path_hiding`.

Regression lines:
  - if the named tool is still resolvable after path_hiding then broken
  - if a tool that shares the hidden tool's directory stops resolving then
    the class of bug PR #2624 fixed is back
  - if path_hiding drops a PATH entry it was not asked to touch then broken
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from tests.scripts._hermetic_path import path_hiding


def _make_exe(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_the_hidden_tool_is_genuinely_absent(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_exe(bin_dir / "widget")
    env_path = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = env_path
    try:
        result = path_hiding(tmp_path, "widget")
    finally:
        os.environ["PATH"] = old_path
    assert shutil.which("widget", path=result) is None, result


def test_a_tool_sharing_the_hidden_tools_directory_still_resolves(tmp_path: Path) -> None:
    """The exact collision from PR #2624: hiding one tool must not hide its neighbor.

    A directory holds both a fake tool being hidden AND a symlink to the
    real bash, the same shape as a self-hosted runner whose Homebrew bin dir
    holds both doppler (or uv, python, sccache) and bash. The naive
    `_path_without` (drop the whole directory) would make bash unresolvable
    too; path_hiding must not.
    """
    real_bash = shutil.which("bash")
    assert real_bash, "no bash on the host PATH; cannot build the scenario"

    shared = tmp_path / "shared-bin"
    shared.mkdir()
    _make_exe(shared / "widget")
    (shared / "bash").symlink_to(real_bash)

    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([str(shared), old_path])
    try:
        result = path_hiding(tmp_path, "widget")
    finally:
        os.environ["PATH"] = old_path

    assert shutil.which("widget", path=result) is None, result
    resolved_bash = shutil.which("bash", path=result)
    assert resolved_bash is not None, (
        "bash must still resolve after hiding a neighbor in the same directory: "
        f"{result}"
    )
    assert str(shared) not in result.split(os.pathsep), (
        "the shared directory itself should have been replaced by a shadow "
        f"copy, not passed through verbatim: {result}"
    )


def test_untouched_path_entries_are_kept_verbatim(tmp_path: Path) -> None:
    other = tmp_path / "unrelated-bin"
    other.mkdir()
    _make_exe(other / "keepme")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([str(other), old_path])
    try:
        result = path_hiding(tmp_path, "an-executable-that-truly-does-not-exist")
    finally:
        os.environ["PATH"] = old_path
    assert str(other) in result.split(os.pathsep)
    assert shutil.which("keepme", path=result) is not None
