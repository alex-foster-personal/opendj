"""Run one program by resolved path; map every failure to a reason string.

The reasons are ``<label>_timed_out``, ``<label>_not_runnable`` and
``<label>_failed:<first stderr line or exit code>``; the caller's ``error``
type turns the reason into its own exception.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable


def run_checked(argv: list[str], *, label: str, timeout_s: float, error: Callable[[str], Exception]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise error(f"{label}_timed_out") from exc
    except OSError as exc:
        raise error(f"{label}_not_runnable") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise error(f"{label}_failed:{err[0] if err else proc.returncode}")
    return proc.stdout.decode("utf-8", errors="replace").strip()


__all__ = ["run_checked"]
