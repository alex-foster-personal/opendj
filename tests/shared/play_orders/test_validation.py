"""PLAY-03 override validators."""
from __future__ import annotations

import pytest

from apps.shared.play_orders.validation import (
    validate_key_sync,
    validate_target_key,
    validate_target_tempo,
)

pytestmark = pytest.mark.requirement("PLAY-03")


@pytest.mark.parametrize(
    "value",
    [None, "1A", "12B", "8A", "Am", "F#m", "C", "Bb"],
)
def test_key_accepts(value: str | None) -> None:
    validate_target_key(value)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "13A", "0A", "8C", "HH", "minor"],
)
def test_key_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        validate_target_key(value)


def test_key_rejects_wrong_type() -> None:
    with pytest.raises(ValueError):
        validate_target_key(123)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, 20.0, 60.0, 128, 128.5, 300.0])
def test_tempo_accepts(value: float | int | None) -> None:
    validate_target_tempo(value)


@pytest.mark.parametrize("value", [0, -1, -5.0, 10.0, 19.9, 300.01, 500])
def test_tempo_rejects(value: float | int) -> None:
    with pytest.raises(ValueError):
        validate_target_tempo(value)


def test_tempo_rejects_string() -> None:
    with pytest.raises(ValueError):
        validate_target_tempo("128")  # type: ignore[arg-type]


def test_tempo_rejects_bool() -> None:
    with pytest.raises(ValueError):
        validate_target_tempo(True)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, True, False])
def test_key_sync_accepts(value: bool | None) -> None:
    validate_key_sync(value)


@pytest.mark.parametrize("value", [0, 1, "yes", "", []])
def test_key_sync_rejects(value: object) -> None:
    with pytest.raises(ValueError):
        validate_key_sync(value)  # type: ignore[arg-type]
