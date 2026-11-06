"""Shared ``gh api`` GET transport for stdlib-only CI scripts."""

from __future__ import annotations

import shutil
import subprocess


def gh_api(path: str, *, timeout: float = 120, error_cls: type[Exception]) -> bytes:
    """Run ``gh api <path>`` and return stdout bytes.

    ``error_cls`` is the caller's domain error (``LedgerError``, ``MapError``).
    ``subprocess.TimeoutExpired`` is not converted.
    """
    gh = shutil.which("gh")
    if gh is None:
        raise error_cls("gh is not on PATH")
    done = subprocess.run([gh, "api", path], capture_output=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise error_cls(
            f"gh api {path} exited {done.returncode}: {done.stderr.decode(errors='replace').strip()}"
        )
    return done.stdout
