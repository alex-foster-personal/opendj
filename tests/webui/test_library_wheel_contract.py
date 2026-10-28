"""apps.library_wheel is reached through its contract (ADR-NEW-module-contracts, M1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.library_wheel import query
from apps.library_wheel.contract import (
    AXES,
    CONTRACT_VERSION,
    AxisInfo,
    LibraryWheelError,
    LibraryWheelService,
)
from apps.library_wheel.http import build_service


def test_built_service_satisfies_the_protocol_and_delegates() -> None:
    service = build_service()
    assert isinstance(service, LibraryWheelService)
    assert service.simple_genre_family("Tech House") == ("tech-house", "#4ad0c8")
    assert service.simple_genre_family("  ") is None


def test_contract_names_are_the_ones_the_package_uses() -> None:
    assert CONTRACT_VERSION == "library_wheel/3"
    assert query.AXES is AXES
    assert query.LibraryWheelError is LibraryWheelError
    assert [a.key for a in AXES][:3] == ["genre", "decade", "play_count"]


def test_axis_info_rejects_extra_fields_and_is_frozen() -> None:
    with pytest.raises(ValidationError):
        AxisInfo(key="k", label="K", enabled=True, reason=None, extra="no")
    axis = AXES[0]
    with pytest.raises(ValidationError):
        axis.key = "other"
