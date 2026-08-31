"""Tests for cross-platform test capability detection."""

from __future__ import annotations

import os

from tests.platform_capabilities import posix_permission_denial_supported


def test_windows_does_not_claim_posix_permission_denial_support() -> None:
    assert posix_permission_denial_supported("nt", None) is False


def test_posix_without_geteuid_does_not_claim_permission_denial_support() -> None:
    assert posix_permission_denial_supported("posix", None) is False


def test_current_process_capability_matches_real_privilege_state() -> None:
    geteuid = getattr(os, "geteuid", None)
    expected = os.name != "nt" and geteuid is not None and geteuid() != 0
    assert posix_permission_denial_supported(os.name, geteuid) is expected
