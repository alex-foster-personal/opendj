"""The user-identity seam answers on both platform arms, including the lock-name token."""

from __future__ import annotations

import os

from apps.shared.user_identity import lock_identity_token, running_as_root


def test_posix_arm_is_root_only_for_uid_zero() -> None:
    assert running_as_root(lambda: 0) is True
    assert running_as_root(lambda: 1000) is False


def test_windows_arm_without_getuid_is_never_root() -> None:
    assert running_as_root(None) is False


def test_default_reads_this_host() -> None:
    getuid = getattr(os, "getuid", None)
    assert running_as_root() is (getuid is not None and getuid() == 0)


def test_posix_lock_token_is_decimal_uid() -> None:
    assert lock_identity_token(lambda: 1000) == "1000"
    assert lock_identity_token(lambda: 0) == "0"


def test_windows_lock_token_uses_username() -> None:
    assert lock_identity_token(None, environ={"USERNAME": "USER"}) == "USER"


def test_windows_lock_token_empty_env_is_nouser() -> None:
    assert lock_identity_token(None, environ={}) == "nouser"


def test_windows_lock_token_sanitizes_unsafe_characters() -> None:
    assert lock_identity_token(None, environ={"USERNAME": "USER NAME"}) == "USER_NAME"


def test_default_lock_token_matches_this_host() -> None:
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        assert lock_identity_token() == str(getuid())
        return
    token = lock_identity_token()
    assert token
    assert "/" not in token
    assert "\\" not in token
