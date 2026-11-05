"""Register analysis lanes whose own producers have landed (PARITY-02).

Producer packages cannot call ``register_serving_lane`` from ``__init__``
without completing an ``analysis`` <-> ``analysis_beatgrid`` cycle, because
``apps.analysis.backends.own_beatgrid`` already imports ``analysis_beatgrid``.
The webui route imports this module once at load time instead.
"""

from __future__ import annotations

from apps.analysis.serving_lanes import SERVING_LANES, register_serving_lane

# ``phrase`` serves own sections from the structure sidecar
# (apps/analysis_structure/serve.py); a track with no sidecar answers an empty
# `phrases` with `phrases_source: none`, never rekordbox's (STRUCT-04).
# ``loudness`` is NATIVE-07 / ``own_loudness.backfill``; selectable because the
# producer landed, not because the persisted default changed (issue #7069).
_LANDED_LANES: tuple[str, ...] = ("beatgrid", "waveform", "phrase", "loudness")


def ensure_analysis_serving_lanes() -> None:
    import apps.analysis_key.serving_lane  # noqa: F401 - PARITY-02 key producer ownership

    register_serving_lane("key")
    for lane in _LANDED_LANES:
        if lane not in SERVING_LANES:
            register_serving_lane(lane)


ensure_analysis_serving_lanes()
