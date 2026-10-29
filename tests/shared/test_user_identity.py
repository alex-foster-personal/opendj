"""The root-detection seam answers on both platform arms (AGENTS.md "Portable by default")."""

from __future__ import annotations

import os

from apps.shared.user_identity import running_as_root


def test_posix_arm_is_root_only_for_uid_zero() -> None:
    assert running_as_root(lambda: 0) is True
    assert running_as_root(lambda: 1000) is False


def test_windows_arm_without_getuid_is_never_root() -> None:
    assert running_as_root(None) is False


def test_default_reads_this_host() -> None:
    getuid = getattr(os, "getuid", None)
    assert running_as_root() is (getuid is not None and getuid() == 0)
