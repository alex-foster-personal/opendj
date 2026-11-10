"""Delivery-layer entry point for ``apps.analysis_structure`` (ADR-NEW-module-contracts).

Delivery code (``apps.webui``, ``apps.engine_core``, ``apps.opendj_cli``) and
``scripts/`` build the service here; everything else imports only the contract.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pathlib import Path

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


@dataclass(frozen=True)
class NicheIntel:
    """HTTP-facing niche label search helpers (issue #11190, parent #345)."""

    read_state: Any
    suggestions_for_track: Any
    suggestions_with_verdicts: Any
    accepted_query_labels: Any
    record_user_region: Any
    set_suggestion_verdict: Any
    niche_find_similar: Any
    normalize_niche_state: Any
    empty_niche_state: Any
    field_name: str

    def view(self, conn, data_dir: Path, stable_id: str) -> dict[str, Any]:
        state = self.read_state(conn, stable_id)
        suggestions = self.suggestions_for_track(data_dir, stable_id)
        return {
            "stable_id": stable_id,
            "suggestions": self.suggestions_with_verdicts(suggestions, state),
            "regions": self.normalize_niche_state(state)["regions"],
            "query_labels": self.accepted_query_labels(state, suggestions),
        }


def build_niche_intel() -> NicheIntel:
    return NicheIntel(
        read_state=pipeline.read_niche_state,
        suggestions_for_track=pipeline.niche_suggestions_for_track,
        suggestions_with_verdicts=pipeline.suggestions_with_verdicts,
        accepted_query_labels=pipeline.accepted_query_labels,
        record_user_region=pipeline.record_user_region,
        set_suggestion_verdict=pipeline.set_suggestion_verdict,
        niche_find_similar=pipeline.niche_find_similar,
        normalize_niche_state=pipeline.normalize_niche_state,
        empty_niche_state=pipeline.empty_niche_state,
        field_name=pipeline.NICHE_LABELS_FIELD,
    )


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
    "NicheIntel",
    "SegmentBench",
    "StructureService",
    "build_niche_intel",
    "build_segment_bench",
    "build_service",
]
