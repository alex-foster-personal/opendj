"""Tests for CloudSync-policy stems executor routing (issue #1866)."""

from __future__ import annotations

import pytest

from apps.stems.routing import (
    EXECUTOR_LOCAL,
    EXECUTOR_MODAL,
    effective_tier,
    resolve_stems_executor,
)


def test_local_policy_selects_local_executor() -> None:
    assert resolve_stems_executor(remote_processing_allowed=False) == EXECUTOR_LOCAL


def test_cloud_policy_with_farm_available_selects_modal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.stems.api.stems_transport_state",
        lambda: ("relay", None),
    )
    assert resolve_stems_executor(remote_processing_allowed=True) == EXECUTOR_MODAL


def test_cloud_policy_falls_back_to_local_when_transport_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.stems.api.stems_transport_state",
        lambda: ("relay", "no relay configured"),
    )
    assert resolve_stems_executor(remote_processing_allowed=True) == EXECUTOR_LOCAL


def test_effective_tier_for_local_executor() -> None:
    assert effective_tier("M", EXECUTOR_LOCAL) == "LOCAL"
    assert effective_tier("M", EXECUTOR_MODAL) == "M"
