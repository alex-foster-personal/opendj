"""Refresh request validation, separate from the ingest route handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

from apps.analysis.backlog import ANALYZER_LANES, AutoLane

TrackAnalysisKind = Literal["vocals", "beatgrid", "key", "stems"]
ORDERABLE_ANALYSIS_KINDS: frozenset[str] = frozenset({"vocals", "beatgrid", "key", "stems"})


class RefreshIn(BaseModel):
    """One refresh scope, including the explicit analysis-grid track order."""

    batch_dir: str | None = None
    scope: Literal["library", "unmapped", "track"] = "library"
    stable_id: str | None = None
    analysis_kind: TrackAnalysisKind | None = None


def resolve_scope(body: RefreshIn | None, ingest_inbox: Path) -> tuple[str, Path | None]:
    """Return the exclusive scope and validated staged-batch directory."""
    if body is None:
        return "library", None
    if body.scope == "track":
        if body.batch_dir is not None:
            raise HTTPException(422, "track scope cannot be combined with batch_dir")
        if body.stable_id is None or body.stable_id.strip() == "":
            raise HTTPException(422, "track scope requires stable_id")
        if body.analysis_kind is None or body.analysis_kind.strip() == "":
            raise HTTPException(422, "track scope requires analysis_kind")
        return "track", None
    if body.stable_id is not None or body.analysis_kind is not None:
        raise HTTPException(422, "stable_id and analysis_kind require scope='track'")
    if body.batch_dir is None:
        return body.scope, None
    if body.scope != "library":
        raise HTTPException(
            422,
            f"batch_dir cannot be combined with scope={body.scope!r}; a job has exactly one scope",
        )
    batch_dir = Path(body.batch_dir).resolve()
    if not batch_dir.is_dir():
        raise HTTPException(422, f"batch_dir not found: {batch_dir}")
    if not batch_dir.is_relative_to(ingest_inbox.resolve()):
        raise HTTPException(422, f"batch_dir must live under {ingest_inbox}")
    return "batch", batch_dir


def unmapped_steps(
    steps: list[str],
    lanes: frozenset[AutoLane],
) -> tuple[list[str], list[str]]:
    """Return runnable unmapped steps for the enabled analyze-on-import lanes."""
    if not lanes:
        raise HTTPException(
            422,
            "every analyze-on-import lane is off (Settings > Analysis: "
            "auto_analyze_grid/key/sections/vocals); a manual order still works",
        )
    run: list[str] = []
    skipped: list[str] = []
    if lanes & ANALYZER_LANES:
        if "analysis" not in steps:
            raise HTTPException(
                422,
                "scope=unmapped runs the analysis step only, and analysis is disabled in the ingest config",
            )
        run.append("analysis")
    elif "analysis" in steps:
        skipped.append("analysis")
    if "vocals" in lanes:
        if "vocals" in steps:
            run.append("vocals")
    elif "vocals" in steps:
        skipped.append("vocals")
    for step in steps:
        if step not in run and step not in skipped:
            skipped.append(step)
    return run, skipped
