"""Tests for apps.shared.posix_shell.resolve_posix_bash.

[if] a POSIX bash (Git for Windows usr\\bin, /bin) is on PATH [then] its absolute
     path is returned and it runs, [else stop]
[if] the only bash on PATH is Windows' System32 WSL launcher but git.exe is on
     PATH [then] Git for Windows' own bin\\bash.exe is returned, [else stop]
[if] the only bash on PATH is the System32 WSL launcher and no git [then] None,
     never the launcher, [else stop]
[if] no bash and no git on PATH [then] None, [else stop]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.posix_shell import resolve_posix_bash

VALID_SCRIPT = "#!/usr/bin/env bash\nset -euo pipefail\necho ok\n"
windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="CreateProcess order and the WSL launcher are Windows-only"
)


def _system32_dir() -> Path:
    return Path(os.environ["SYSTEMROOT"]) / "System32"


def _require_wsl_launcher() -> Path:
    launcher = _system32_dir() / "bash.exe"
    if not launcher.exists():
        pytest.skip(f"{launcher} absent (WSL feature not installed); the hazard cannot occur")
    return launcher


def _require_git_cmd_dir() -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    return Path(git).parent


@pytest.mark.requirement("CROSS-02")
def test_resolve_posix_bash_returns_an_absolute_runnable_bash() -> None:
    """[if] a POSIX bash is installed [then] an absolute, runnable path is returned, [else stop]."""
    bash = resolve_posix_bash()
    if bash is None and shutil.which("git") is None and shutil.which("bash") is None:
        pytest.skip("neither bash nor git on PATH")
    assert bash is not None, "if resolve_posix_bash misses an installed bash then broken"
    assert Path(bash).is_absolute(), f"if resolve_posix_bash returns a bare name then broken: {bash}"
    proc = subprocess.run([bash, "-c", "echo $BASH_VERSION"], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0 and proc.stdout.strip(), f"if the resolved bash cannot run then broken: {proc.stderr}"


@pytest.mark.requirement("CROSS-02")
@windows_only
def test_resolve_posix_bash_finds_git_bash_beside_the_wsl_launcher(monkeypatch) -> None:
    """[if] PATH has only System32 and git [then] Git's own bash is returned, [else stop]."""
    # The default Git for Windows install puts only Git\cmd on PATH, so which()
    # alone sees System32's launcher; Git's own bin\bash.exe must be found.
    _require_wsl_launcher()
    git_cmd_dir = _require_git_cmd_dir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(_system32_dir()), str(git_cmd_dir)]))
    which_bash = Path(shutil.which("bash") or "")
    assert which_bash.parent == _system32_dir(), "control: which() must see the launcher"
    bash = resolve_posix_bash()
    assert bash is not None, "if resolve_posix_bash misses Git's bash beside git.exe then broken"
    assert Path(bash).parent.name.casefold() != "system32", f"if WSL launcher then broken: {bash}"
    proc = subprocess.run([bash, "-c", "true"], capture_output=True, timeout=30, check=False)
    assert proc.returncode == 0, f"if the Git bash it found cannot run then broken: {proc.stderr!r}"


@pytest.mark.requirement("CROSS-02")
@windows_only
def test_resolve_posix_bash_refuses_a_lone_wsl_launcher(monkeypatch) -> None:
    """[if] the only bash is the System32 WSL launcher [then] None, [else stop]."""
    _require_wsl_launcher()
    monkeypatch.setenv("PATH", str(_system32_dir()))
    assert shutil.which("bash") is not None, "control: which() must see the launcher on this PATH"
    assert shutil.which("git") is None, "control: no git on this PATH"
    assert resolve_posix_bash() is None, "if resolve_posix_bash returns the WSL launcher then broken"


@pytest.mark.requirement("CROSS-02")
@windows_only
def test_resolve_posix_bash_ignores_binaries_planted_in_the_cwd(monkeypatch, tmp_path) -> None:
    """[if] bash.exe and git.exe are planted in the CWD [then] neither is used, [else stop]."""
    # shutil.which on Windows (3.11) searches the CWD before PATH and returns a
    # relative .\bash.EXE, so a planted binary would be executed.
    _require_wsl_launcher()
    git_cmd_dir = _require_git_cmd_dir()
    planted = _system32_dir() / "whoami.exe"
    for name in ("bash.exe", "git.exe"):
        shutil.copyfile(planted, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([str(_system32_dir()), str(git_cmd_dir)]))
    assert Path(shutil.which("bash") or "").resolve().parent == tmp_path.resolve(), (
        "control: which() must pick the planted CWD binary on this Python"
    )
    bash = resolve_posix_bash()
    assert bash is not None and Path(bash).is_absolute(), f"if not absolute then broken: {bash}"
    assert Path(bash).resolve().parent != tmp_path.resolve(), f"if a CWD-planted binary is resolved then broken: {bash}"


@pytest.mark.requirement("CROSS-02")
def test_resolve_posix_bash_is_none_with_no_bash_or_git_on_path(monkeypatch, tmp_path) -> None:
    """[if] neither bash nor git is on PATH [then] None, [else stop]."""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert shutil.which("bash") is None and shutil.which("git") is None, "control: empty PATH"
    assert resolve_posix_bash() is None, "if it invents a bash with none installed then broken"


@pytest.mark.requirement("CROSS-02")
@windows_only
def test_bare_bash_argv0_bypasses_path_on_windows(tmp_path) -> None:
    """[if] bare bash argv0 runs with Git bash first on PATH [then] WSL runs, [else stop]."""
    # Hazard control: proves WHY callers must pass a resolved path. With a
    # working bash FIRST on PATH, a bare "bash" argv0 still runs System32's WSL
    # launcher (CreateProcess searches System32 before PATH), which cannot read
    # a Windows path. If this stops reproducing, the premise needs revisiting.
    _require_wsl_launcher()
    working = resolve_posix_bash()
    if working is None:
        pytest.skip("no POSIX bash installed to compare against")
    good = tmp_path / "good.sh"
    good.write_text(VALID_SCRIPT, encoding="utf-8")
    path_first = {
        **os.environ,
        "PATH": os.pathsep.join([str(Path(working).parent), os.environ["PATH"]]),
    }
    resolved = subprocess.run([working, "-n", str(good)], capture_output=True, timeout=30, check=False, env=path_first)
    bare = subprocess.run(["bash", "-n", str(good)], capture_output=True, timeout=30, check=False, env=path_first)
    assert resolved.returncode == 0, f"control: resolved bash must accept: {resolved.stderr!r}"
    assert bare.returncode != 0, "if bare 'bash' resolves through PATH then this premise is stale"
