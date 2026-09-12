"""``apps.analysis_key.serving_lane`` registers the key lane at import time."""
from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest

from apps.analysis.serving_lanes import SERVING_LANES


@pytest.fixture()
def _restore_key_serving_lane() -> Iterator[None]:
    had_key = "key" in SERVING_LANES
    yield
    if had_key:
        SERVING_LANES.add("key")
    else:
        SERVING_LANES.discard("key")


def test_register_serving_lane_key(_restore_key_serving_lane: None) -> None:
    import apps.analysis_key.serving_lane as serving_lane_mod

    SERVING_LANES.discard("key")
    importlib.reload(serving_lane_mod)
    assert "key" in SERVING_LANES
