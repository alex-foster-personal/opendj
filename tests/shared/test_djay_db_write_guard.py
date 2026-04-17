"""Regression tests for the djay_db write-path safety contract.

Ties to INFRA-02 / P2 forensics item ``refactor/shared-djay-db-write-guard``.

``apps/shared/djay_db`` is a read-only reader today, but it has many inbound
importers. This test pins the invariant that any future write-capable function
must be registered in ``_REQUIRES_SAFETY_SESSION`` AND must accept a
``safety_session`` parameter, so writes cannot silently bypass the safety rails
in ``apps/sync``.
"""
from __future__ import annotations

import inspect

import pytest

from apps.shared import djay_db

pytestmark = pytest.mark.requirement("INFRA-02")


def test_contract_constant_exists_and_is_frozenset() -> None:
    assert hasattr(djay_db, "_REQUIRES_SAFETY_SESSION")
    assert isinstance(djay_db._REQUIRES_SAFETY_SESSION, frozenset)


def test_module_is_read_only_today() -> None:
    """Snapshot: the set is empty today. Flipping this requires deliberate
    action (add the name AND add ``safety_session`` to the signature)."""
    assert djay_db._REQUIRES_SAFETY_SESSION == frozenset(), (
        "A write-capable function was added. Ensure it routes through the "
        "apps/sync safety session, then update this snapshot."
    )


def test_every_listed_function_takes_safety_session() -> None:
    """Enforcement: if new names get added, they must accept safety_session."""
    for name in djay_db._REQUIRES_SAFETY_SESSION:
        fn = getattr(djay_db, name, None)
        assert callable(fn), f"{name!r} listed but not defined in djay_db"
        params = inspect.signature(fn).parameters
        assert "safety_session" in params, (
            f"{name!r} is in _REQUIRES_SAFETY_SESSION but does not accept a "
            f"'safety_session' parameter"
        )


def test_import_time_enforcer_rejects_missing_function() -> None:
    """The module-level enforcer must raise for a bogus entry."""
    original = djay_db._REQUIRES_SAFETY_SESSION
    try:
        djay_db._REQUIRES_SAFETY_SESSION = frozenset({"definitely_not_a_real_fn"})
        with pytest.raises(RuntimeError, match="not defined"):
            djay_db._enforce_write_path_contract()
    finally:
        djay_db._REQUIRES_SAFETY_SESSION = original


def test_import_time_enforcer_rejects_missing_safety_session_param() -> None:
    """A registered function without ``safety_session`` must raise."""
    original = djay_db._REQUIRES_SAFETY_SESSION
    try:
        # ``iter_tracks`` is a real read-only function lacking safety_session.
        djay_db._REQUIRES_SAFETY_SESSION = frozenset({"iter_tracks"})
        with pytest.raises(RuntimeError, match="safety_session"):
            djay_db._enforce_write_path_contract()
    finally:
        djay_db._REQUIRES_SAFETY_SESSION = original
