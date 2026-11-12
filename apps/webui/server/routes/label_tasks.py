"""In-app labelling tasks: stem A/B/C rank and batch consensus (#11193, #363).

GET exposes clip URLs that stream through the existing stem part routes.
POST stores one response per ``(task_id, anon_session)``. GET consensus
aggregates only once ``batch_size`` responses exist; otherwise the caller
gets ``insufficient_labels`` and no invented ranking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from apps.shared.state import db as state_db
from apps.stems.artifacts import StemArtifactError
from apps.webui.server.label_tasks_domain import (
    MODALITY_STEMS_AB_RANK,
    LabelTaskError,
    build_consensus,
    fetch_responses,
    insert_response,
    load_task,
    parse_stems_ab_payload,
    resolve_clip_urls,
    wait_window_expired,
)
from apps.webui.server.deps import get_library_state_db, get_write_state
from apps.webui.server.routes.stems_bundle import _stem_roots, _stems_dir

from ..backend import StateBackend

router = APIRouter(prefix="/label-tasks", tags=["label-tasks"])
MOUNT_UNDER_API_PREFIX = True


class LabelClipOut(BaseModel):
    id: str
    stable_id: str
    part: str
    url: str


class StemAbRankTaskOut(BaseModel):
    task_id: str
    experiment_id: str
    modality: str
    reward_karma: int
    batch_size: int
    max_wait_s: int
    payload: dict[str, Any]
    clips: list[LabelClipOut]


class LabelResponseIn(BaseModel):
    anon_session: str = Field(min_length=1)
    ranking: list[str] = Field(min_length=3, max_length=3)
    strength: float = Field(ge=0.0, le=1.0)
    cost_choice: str = Field(min_length=1)
    ms_to_complete: int = Field(ge=0)
    user_id: str | None = None


class LabelResponseOut(BaseModel):
    response_id: str
    karma_granted: bool = True


class LabelConsensusOut(BaseModel):
    task_id: str
    experiment_id: str
    status: str
    response_count: int
    batch_size: int
    max_wait_s: int
    wait_elapsed_s: int
    reason: str | None = None
    consensus: dict[str, Any] | None = None


def _stems_dir_from_request(request: Request) -> Path:
    return _stems_dir(request)


def _roots_from_request(request: Request) -> tuple[Path, ...] | None:
    return _stem_roots(request)


@router.get("/{task_id}", response_model=StemAbRankTaskOut)
def get_label_task(task_id: str, request: Request) -> StemAbRankTaskOut:
    """Return a stems_ab_rank task with clip URLs backed by validated bundles."""
    conn = state_db.open_ro(get_library_state_db(request))
    try:
        task = load_task(conn, task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found") from None
    except LabelTaskError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()
    if task.modality != MODALITY_STEMS_AB_RANK:
        raise HTTPException(status_code=422, detail=f"unsupported modality {task.modality!r}")
    try:
        clips = parse_stems_ab_payload(task.payload)
        resolved = resolve_clip_urls(
            clips,
            stems_dir=_stems_dir_from_request(request),
            roots=_roots_from_request(request),
        )
    except (LabelTaskError, StemArtifactError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return StemAbRankTaskOut(
        task_id=task.id,
        experiment_id=task.experiment_id,
        modality=task.modality,
        reward_karma=task.reward_karma,
        batch_size=task.batch_size,
        max_wait_s=task.max_wait_s,
        payload=task.payload,
        clips=[LabelClipOut(**row) for row in resolved],
    )


@router.post(
    "/{task_id}/responses",
    response_model=LabelResponseOut,
    status_code=status.HTTP_201_CREATED,
)
def post_label_response(
    task_id: str,
    body: LabelResponseIn,
    request: Request,
    _write_guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> LabelResponseOut:
    """Store ranking, strength, and cost_choice; karma is granted in the ledger."""
    del _write_guard
    conn = state_db.open_rw(get_library_state_db(request))
    try:
        response_id = insert_response(
            conn,
            task_id=task_id,
            anon_session=body.anon_session,
            ranking=body.ranking,
            strength=body.strength,
            cost_choice=body.cost_choice,
            ms_to_complete=body.ms_to_complete,
            user_id=body.user_id,
        )
    except KeyError:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found") from None
    except LabelTaskError as exc:
        conn.close()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        conn.close()
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="response already recorded for this session") from exc
        raise
    conn.close()
    return LabelResponseOut(response_id=response_id)


@router.get("/{task_id}/consensus", response_model=LabelConsensusOut)
def get_label_consensus(task_id: str, request: Request) -> LabelConsensusOut:
    """Aggregate batch responses; incomplete batches return insufficient_labels."""
    conn = state_db.open_ro(get_library_state_db(request))
    try:
        task = load_task(conn, task_id)
        responses = fetch_responses(conn, task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found") from None
    except LabelTaskError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()
    result = build_consensus(task, responses)
    if result["status"] == "insufficient_labels" and wait_window_expired(task):
        result = {**result, "reason": "max_wait_s elapsed with fewer than batch_size responses"}
    consensus = result.get("consensus")
    return LabelConsensusOut(
        task_id=result["task_id"],
        experiment_id=result["experiment_id"],
        status=result["status"],
        response_count=result["response_count"],
        batch_size=result["batch_size"],
        max_wait_s=result["max_wait_s"],
        wait_elapsed_s=result["wait_elapsed_s"],
        reason=result.get("reason"),
        consensus=consensus if isinstance(consensus, dict) else None,
    )


__all__ = ["router"]
