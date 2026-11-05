"""Resolve a POSIX bash to an absolute path; never launch a bare ``"bash"``.

On Windows, ``subprocess.run(["bash", ...])`` hands the bare name to
CreateProcess, whose search order checks System32 BEFORE the PATH variable.
System32 ships ``bash.exe``, the WSL launcher, so a bare argv0 runs WSL even
when Git for Windows' bash is first on PATH. WSL bash cannot open a Windows
path (it reads ``C:\\Users\\x\\r.sh`` as ``C:Usersxr.sh``) and, with no distro
installed, cannot run at all.

``shutil.which`` searches PATH only, but System32 is on every Windows PATH and
the default Git for Windows install adds only ``Git\\cmd``, so on a typical
machine ``which("bash")`` returns that same launcher. Resolution order on
Windows is therefore: a PATH bash that is not the System32 launcher (Git's
``usr\\bin`` on CI, MSYS2), else Git for Windows' own ``bin\\bash.exe`` found
beside ``git.exe``, else None. On POSIX it is ``which("bash")``.

Only ``bash`` has this hazard: System32 ships no ``sh.exe``, so a bare ``"sh"``
falls through to PATH. Measured on a Windows 11 host, Wed 14 Oct 2026.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _which_on_absolute_path(name: str) -> str | None:
    """``shutil.which`` restricted to ABSOLUTE PATH entries, never the CWD.

    On Windows, ``shutil.which`` (3.11) searches the current directory before
    PATH and returns a relative ``.\\bash.EXE``, so a ``bash.exe`` planted in
    whatever directory the caller runs from would be executed. Empty and
    relative PATH entries are skipped for the same reason.
    """
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        if sys.platform == "win32":
            candidate = Path(entry) / f"{name}.exe"
            if candidate.is_file():
                return str(candidate)
        elif (found := shutil.which(name, path=entry)) is not None:
            return found
    return None


def _is_wsl_launcher(path: str) -> bool:
    return Path(path).parent.name.casefold() == "system32"


def _git_for_windows_bash() -> str | None:
    """``<Git>\\bin\\bash.exe`` for the git.exe on PATH (cmd\\ or mingw64\\bin\\)."""
    git = _which_on_absolute_path("git")
    if git is None:
        return None
    for ancestor in Path(git).resolve().parents:
        candidate = ancestor / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def resolve_posix_bash() -> str | None:
    """Absolute path of a POSIX bash, or None when none is installed.

    Callers fail closed on None (report unavailable, or skip a test); they
    never fall back to a bare ``"bash"``, which on Windows runs WSL.
    """
    found = _which_on_absolute_path("bash")
    if sys.platform != "win32":
        return found
    if found is not None and not _is_wsl_launcher(found):
        return found
    return _git_for_windows_bash()


__all__ = ["resolve_posix_bash"]
