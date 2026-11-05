"""Small cross-platform capability checks shared by test modules."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from apps.shared.posix_shell import resolve_posix_bash


def posix_bash_or_skip() -> str:
    """Absolute path of a POSIX bash for a test's subprocess argv0, else skip.

    Use this instead of a bare ``"bash"``: on Windows a bare argv0 runs the
    System32 WSL launcher even with Git bash first on PATH (see
    apps.shared.posix_shell). Fails closed as a named skip, never a bare name.
    """
    bash = resolve_posix_bash()
    if bash is None:
        pytest.skip(
            "no POSIX bash installed (Git for Windows, /bin); "
            "the System32 WSL launcher is refused"
        )
    return bash


def posix_permission_denial_supported(
    os_name: str, geteuid: Callable[[], int] | None
) -> bool:
    """Whether chmod can create a real unreadable-directory fixture."""
    return os_name != "nt" and geteuid is not None and geteuid() != 0
