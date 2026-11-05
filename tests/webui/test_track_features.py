"""Focused tests for shared Track -> TrackFeature projection helpers."""

from __future__ import annotations

import pytest

from apps.webui.server.backend import Provenance, Track
from apps.webui.server.track_features import (
    camelot_or_none,
    energy_of,
    track_to_feature,
)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_camelot_or_none_missing_or_blank(key: str | None) -> None:
    assert camelot_or_none(key) is None


def test_camelot_or_none_invalid_key() -> None:
    assert camelot_or_none("not-a-key") is None


@pytest.mark.parametrize("key", ["8A", "Am"])
def test_camelot_or_none_valid_keys(key: str) -> None:
    assert camelot_or_none(key) == "8A"


def test_energy_of_no_provenance() -> None:
    track = Track(stable_id="t1")
    assert energy_of(track) is None


def test_energy_of_with_energy_provenance() -> None:
    track = Track(
        stable_id="t2",
        provenance={
            "energy": Provenance("7", "mik", None, "2026-01-01T00:00:00Z", "ok"),
        },
    )
    assert energy_of(track) == 7


def test_track_to_feature_projection() -> None:
    track = Track(
        stable_id="sid-1",
        artist="Artist",
        bpm=128.0,
        key="Am",
        provenance={
            "energy": Provenance(5, "mik", None, "2026-01-01T00:00:00Z", "ok"),
        },
    )
    feature = track_to_feature(track)
    assert feature.stable_id == "sid-1"
    assert feature.artist == "Artist"
    assert feature.bpm == 128.0
    assert feature.key_camelot == "8A"
    assert feature.energy == 5


def test_track_to_feature_blank_key_yields_none_camelot() -> None:
    track = Track(stable_id="sid-2", key="  ", bpm=120.0)
    feature = track_to_feature(track)
    assert feature.key_camelot is None
