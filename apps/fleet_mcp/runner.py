"""Subprocess execution with honest failure semantics.

``.claude/rules/verification.md``: a tool that cannot measure must report
UNKNOWN, never a verdict, and a count-based check needs stderr VISIBLE. So
every command this package runs goes through :func:`run` and comes back as a
:class:`Completed` that keeps ``stderr`` and distinguishes three outcomes:

* ``measured``  -- the command ran and exited; ``returncode`` is its verdict
* ``UNKNOWN``   -- the command could not be run or did not finish (missing
  binary, timeout, transport failure). NOT a pass and NOT a fail.

Callers never see a bare empty string where a failure happened, which is the
zero-is-both-a-value-and-an-error-signature case the rule names.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Completed:
    """One command's outcome, with the failure signal preserved."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    unknown_reason: str | None = None

    @property
    def measured(self) -> bool:
        """True when the command actually ran to an exit code."""
        return self.unknown_reason is None

    @property
    def ok(self) -> bool:
        """True only for a command that ran AND exited zero."""
        return self.measured and self.returncode == 0

    def unknown_document(self) -> dict[str, Any]:
        """The UNKNOWN envelope a tool returns instead of inventing a verdict."""
        return {
            "status": UNKNOWN,
            "reason": self.unknown_reason,
            "command": self.argv[0] if self.argv else "",
            "stderr": self.stderr[:1000],
        }

    def failure_document(self) -> dict[str, Any]:
        """The envelope for a command that ran and failed."""
        return {
            "status": "error",
            "returncode": self.returncode,
            "command": self.argv[0] if self.argv else "",
            "stderr": self.stderr[:1000],
        }


class Runner(Protocol):
    """Injection seam: tests supply a fake, the server supplies :func:`run`."""

    def __call__(self, argv: Sequence[str], *, timeout: float) -> Completed: ...


def run(argv: Sequence[str], *, timeout: float) -> Completed:
    """Run ``argv`` with no shell, returning a :class:`Completed`.

    ``argv`` is always a list, never a string: nothing this package runs is
    parsed by a shell on the near side, so a caller-supplied title or body
    cannot become a command.
    """
    argv = tuple(argv)
    if not argv:
        raise ValueError("argv must not be empty")
    if shutil.which(argv[0]) is None:
        return Completed(
            argv=argv,
            returncode=None,
            stdout="",
            stderr="",
            unknown_reason=f"{argv[0]} is not on PATH",
        )
    try:
        proc = subprocess.run(  # fixed argv, shell=False
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Completed(
            argv=argv,
            returncode=None,
            stdout="",
            stderr="",
            unknown_reason=f"{argv[0]} did not finish within {timeout:g}s",
        )
    except OSError as error:
        return Completed(
            argv=argv,
            returncode=None,
            stdout="",
            stderr=str(error),
            unknown_reason=f"{argv[0]} could not be executed: {error}",
        )
    return Completed(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
