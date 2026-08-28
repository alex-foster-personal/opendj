"""Owner-only file access, expressed once per platform.

POSIX carries the intent in the mode bits: ``0o600`` and nothing else can
read the file. Windows has no mode bits at all -- ``os.open(..., 0o600)``
there only toggles the read-only attribute, which is why a file created
that way stats as ``0o666`` on a Windows runner. The capability itself is
present as a discretionary ACL, so this module sets and verifies THAT
rather than pretending the POSIX bits carried over.

Fail-fast contract: a restriction that cannot be applied raises
:class:`PrivateFileError`. Nothing here reports success for a file that is
still world-readable.
"""

from __future__ import annotations

import csv
import os
import stat
import subprocess
from pathlib import Path

OWNER_ONLY_MODE = 0o600
# icacls rights for "read and write, nothing else". Delete/chown stay out.
_WINDOWS_OWNER_RIGHTS = "(R,W)"


class PrivateFileError(RuntimeError):
    """An owner-only restriction could not be applied or read back."""


# ----- Windows ------------------------------------------------------------


def _run(argv: list[str]) -> str:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PrivateFileError(f"{argv[0]} failed: {detail or completed.returncode}")
    return completed.stdout


def _current_user_sid() -> str:
    """SID of the account this process runs as.

    Granted by SID, not by name: ``icacls`` name resolution depends on the
    machine's domain membership and on the display language, while a SID is
    the same string everywhere.
    """
    raw = _run(["whoami", "/user", "/fo", "csv", "/nh"]).strip()
    rows = list(csv.reader(raw.splitlines()))
    if not rows or len(rows[0]) < 2:
        raise PrivateFileError(f"could not read this account's SID: {raw!r}")
    sid = rows[0][1].strip()
    if not sid.startswith("S-1-"):
        raise PrivateFileError(f"whoami returned no SID, got {sid!r}")
    return sid


def _current_user_name() -> str:
    return _run(["whoami"]).strip().casefold()


def _windows_ace_principals(path: Path) -> list[str]:
    """Every principal named in ``path``'s ACL, as icacls prints them.

    icacls writes the first ACE on the same line as the echoed path and one
    per line after that, then a blank line before its summary.
    """
    text = _run(["icacls", str(path)])
    principals: list[str] = []
    for index, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line:
            break
        if index == 0:
            if not line.startswith(str(path)):
                raise PrivateFileError(f"unexpected icacls output for {path}: {raw!r}")
            line = line[len(str(path)) :].strip()
        principal, separator, _rights = line.partition(":(")
        if not separator:
            raise PrivateFileError(f"unparsable icacls entry for {path}: {raw!r}")
        principals.append(principal.strip().casefold())
    return principals


def _restrict_windows(path: Path) -> None:
    """Drop inherited access and grant this account alone read/write."""
    _run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{_current_user_sid()}:{_WINDOWS_OWNER_RIGHTS}",
        ]
    )


def _is_owner_only_windows(path: Path) -> bool:
    principals = _windows_ace_principals(path)
    if len(principals) != 1:
        return False
    granted = principals[0]
    return granted in {_current_user_name(), f"*{_current_user_sid()}".casefold()}


# ----- public API ---------------------------------------------------------


def restrict_to_owner(path: Path) -> None:
    """Restrict ``path`` so only this account can read or write it."""
    if not path.is_file():
        raise PrivateFileError(f"cannot restrict a file that is not there: {path}")
    if os.name == "nt":
        _restrict_windows(path)
    else:
        os.chmod(path, OWNER_ONLY_MODE)


def is_owner_only(path: Path) -> bool:
    """True when nothing but this account can reach ``path``."""
    if os.name == "nt":
        return _is_owner_only_windows(path)
    return stat.S_IMODE(path.stat().st_mode) == OWNER_ONLY_MODE
