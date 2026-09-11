"""Lanes whose own producer has landed and may be selected over HTTP/UI.

Each producer package registers itself via ``register_serving_lane`` from a
small side module imported by the webui route (not from ``analysis_beatgrid``
``__init__``, which ``apps.analysis.backends`` already imports and would
complete an ``analysis`` <-> ``analysis_beatgrid`` package cycle).
"""

from __future__ import annotations

from .lanes import LANES

SERVING_LANES: set[str] = set()


def register_serving_lane(lane: str) -> None:
    if lane not in LANES:
        raise ValueError(f"unknown lane {lane!r}")
    SERVING_LANES.add(lane)


def serving_lanes() -> frozenset[str]:
    return frozenset(SERVING_LANES)
