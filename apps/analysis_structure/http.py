"""Delivery-layer entry point for ``apps.analysis_structure`` (ADR-NEW-module-contracts).

Delivery code (``apps.webui``, ``apps.engine_core``, ``apps.opendj_cli``) and
``scripts/`` build the service here; everything else imports only the contract.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import bench, fast_segment, pipeline
from .contract import CONTRACT_VERSION, PHRASE_SOURCE_LANES, StructureService
from .service import LocalStructureService


def build_service() -> StructureService:
    return LocalStructureService()


@dataclass(frozen=True)
class SegmentBench:
    """The package's own segmenters and bench scorers, for composition roots that run them.

    The segment viewer (``scripts/structure/segment_viewer``) runs both producers on a
    track and scores them against each other. It is a composition root, so it reaches
    them here rather than importing ``bench``, ``fast_segment`` or ``pipeline``
    (ADR-NEW-module-contracts rule 1).
    """

    grid_id: dict[str, str]
    tolerances_s: tuple[float, ...]
    own_fast_version: str
    allin1_version: str
    segment: Callable[..., dict[str, Any]]
    build_sidecar: Callable[..., Any]
    inner: Callable[[list[float], float], list[float]]
    frames: Callable[[list[dict[str, Any]], float], list[str | None]]
    boundary_prf: Callable[[list[float], list[float], float], tuple[float, float, float]]
    frame_counts: Callable[[list[str | None], list[str | None]], dict[str, int]]
    agreement_from_counts: Callable[[dict[str, int]], tuple[float | None, float | None]]
    pooled_agreement: Callable[[list[dict[str, Any]], str], tuple[float | None, float | None, int]]


def build_segment_bench() -> SegmentBench:
    return SegmentBench(
        grid_id=bench.GRID_ID,
        tolerances_s=bench.TOLERANCES_S,
        own_fast_version=fast_segment.PRODUCER_VERSION,
        allin1_version=pipeline.PRODUCER_VERSION,
        segment=fast_segment.segment,
        build_sidecar=pipeline.build_sidecar,
        inner=bench._inner,
        frames=bench._frames,
        boundary_prf=bench.boundary_prf,
        frame_counts=bench.frame_counts,
        agreement_from_counts=bench.agreement_from_counts,
        pooled_agreement=bench.pooled_agreement,
    )


__all__ = [
    "CONTRACT_VERSION",
    "PHRASE_SOURCE_LANES",
    "SegmentBench",
    "StructureService",
    "build_segment_bench",
    "build_service",
]
