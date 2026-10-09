"""The lane vocabulary: names, statuses, producers, and the error type.

A leaf module with no intra-package imports, so :mod:`apps.analysis.lanes`
and :mod:`apps.analysis.lane_payloads` can both depend on it without a
cycle. It exists because those two were one file until the payload rules
grew past the 600-line limit; splitting them needed a shared base.

-Claude
"""
from __future__ import annotations

from typing import Literal

Lane = Literal["beatgrid", "key", "waveform", "loudness", "vocal"]
LANES: tuple[Lane, ...] = ("beatgrid", "key", "waveform", "loudness", "vocal")

# Lanes a user can pick a source for in the PARITY-02 dropdown that are NOT
# record-contract lanes. ``phrase`` is served from the structure sidecar
# (``apps/analysis_structure``), which has not earned a lane in the record
# contract yet (ADR "structure and genre sidecars"), so it gets a source
# selection without a payload validator, canonical pointer or projection.
SELECTION_ONLY_LANES: tuple[str, ...] = ("phrase",)
SELECTION_LANES: tuple[str, ...] = (*LANES, *SELECTION_ONLY_LANES)

LaneStatus = Literal["ok", "failed", "missing"]
LANE_STATUSES: tuple[LaneStatus, ...] = ("ok", "failed", "missing")

Producer = Literal["inapp", "backfill", "cand"]
PRODUCERS: tuple[Producer, ...] = ("inapp", "backfill", "cand")

OWN_BACKEND_PREFIX = "own_"


class LaneContractError(ValueError):
    """A lane block violates the native-analysis v1 record contract."""


__all__ = [
    "LANES",
    "LANE_STATUSES",
    "OWN_BACKEND_PREFIX",
    "PRODUCERS",
    "SELECTION_LANES",
    "SELECTION_ONLY_LANES",
    "Lane",
    "LaneContractError",
    "LaneStatus",
    "Producer",
]
