"""Shared fixtures for apps/entitlements tests."""

from __future__ import annotations

import pytest

from apps.entitlements import PROVIDER_ENV


@pytest.fixture(autouse=True)
def _no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every entitlements test to the shipped inert provider env."""
    monkeypatch.delenv(PROVIDER_ENV, raising=False)
