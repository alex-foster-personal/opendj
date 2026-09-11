"""Register analysis lanes whose own producers have landed (PARITY-02).

Producer packages cannot call ``register_serving_lane`` from ``__init__``
without completing an ``analysis`` <-> ``analysis_beatgrid`` cycle, because
``apps.analysis.backends.own_beatgrid`` already imports ``analysis_beatgrid``.
The webui route imports this module once at load time instead.
"""

from __future__ import annotations

from apps.analysis.serving_lanes import SERVING_LANES, register_serving_lane

_LANDED_LANES: tuple[str, ...] = ("beatgrid", "key")


def ensure_analysis_serving_lanes() -> None:
    for lane in _LANDED_LANES:
        if lane not in SERVING_LANES:
            register_serving_lane(lane)


ensure_analysis_serving_lanes()
