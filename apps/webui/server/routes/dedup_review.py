"""Duplicate-cluster review routes with identity-bound, CAS-guarded decisions.

``GET /dedup/clusters`` exposes the derived duplicate clusters and the exact
decision-store revision. ``POST /dedup/clusters/{cluster_id}/decision`` records
a pending decision. ``POST .../apply`` rewrites OpenDJ playlist memberships
via PlaylistStore; ``POST .../undo`` restores the journaled ``before`` lists.
These routes never invoke ``apps.dedup.apply``, write Rekordbox ``master.db``,
or delete an audio file.

The numeric SQLite cluster id is display-only. Decisions are keyed by a stable
SHA-256 digest of the cluster member stable ids. Every write requires
``If-Match`` and is a locked read-modify-replace cycle.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

from apps.shared import paths as dedup_paths
from apps.shared.events import publish

from ..backend import StateBackend
from ..dedup_cue_presence import CuePresence, bulk_cue_presence
from ..dedup_decisions import (
    DECISIONS_FILE,
    DecisionRevisionConflict,
    InvalidDecisionStoreError,
    PersistedDecision,
    commit_decision_store,
    decision_file_lock,
)
from ..dedup_review_ops import (
    cluster_key,
    cluster_member_ids,
    cluster_needs_manual_review,
    conflict,
    cue_loss_confirmation_required,
    cue_loss_report,
    decision_store_error,
    dedup_db_path,
    drop_apply_journal,
    hydrate_member,
    load_cluster_for_write,
    now_iso,
    persist_apply_journal,
    playlist_name,
    prepare_merge_apply,
    prepare_merge_undo,
    read_raw_clusters,
    read_store_or_http,
    reject_duplicate_cluster_keys,
    require_identity,
)
from ..deps import get_read_state, get_write_state
from ..playlist_store import PlaylistStore
from .playlist_write import get_playlist_store

router = APIRouter(prefix="/dedup", tags=["dedup-review"])

# Re-export so existing tests can monkeypatch this name or the decisions module.
_decision_file_lock = decision_file_lock

ActionLiteral = Literal["merge", "keep-all", "skip"]

_ETAG_RESPONSE_HEADER: dict[str, dict[str, Any]] = {
    "ETag": {
        "description": "Strong validator for the exact decision-store bytes",
        "schema": {"type": "string"},
    },
}
_GET_RESPONSES: dict[int, dict[str, Any]] = {
    200: {"headers": _ETAG_RESPONSE_HEADER},
    500: {"description": "The persisted decision store is unreadable or invalid"},
}
_POST_RESPONSES: dict[int, dict[str, Any]] = {
    200: {"headers": _ETAG_RESPONSE_HEADER},
    409: {
        "description": "The decision revision or stable cluster identity is stale",
        "headers": _ETAG_RESPONSE_HEADER,
    },
    428: {"description": "If-Match is required for every decision write"},
}
_IF_MATCH_OPENAPI_PARAMETER: dict[str, Any] = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "description": "Decision-store ETag returned by GET /api/v1/dedup/clusters",
    "schema": {"type": "string"},
}


class MemberOut(BaseModel):
    stable_id: str
    path: str
    is_canonical: bool
    similarity: float | None
    title: str | None
    artist: str | None
    bpm: float | None
    key: str | None
    duration_ms: int | None
    rating: int | None
    file_exists: bool
    cue_count: int
    hot_cue_count: int
    loop_count: int
    has_beatgrid: bool
    cue_positions_ms: list[int]


class DecisionOut(BaseModel):
    cluster_key: str
    survivor: str
    action: ActionLiteral
    decided_at: str
    pending_apply: bool


class ClusterOut(BaseModel):
    cluster_id: int
    cluster_key: str
    survivor_stable_id: str
    rationale: str | None
    flagged_manual_review: bool
    members: list[MemberOut]
    decision: DecisionOut | None = None


class ClustersOut(BaseModel):
    clusters: list[ClusterOut]
    revision: str
    note: str | None = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_key: str
    survivor: str
    action: ActionLiteral


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_key: str
    survivor: str
    confirm_cue_loss: bool = False


class DecisionRecordOut(BaseModel):
    cluster_id: int
    cluster_key: str
    survivor: str
    action: ActionLiteral
    decided_at: str
    revision: str
    pending_apply: bool = True


class PlaylistRewriteOut(BaseModel):
    playlist_id: str
    name: str
    before: list[str]
    after: list[str]


class ApplyRecordOut(BaseModel):
    cluster_id: int
    cluster_key: str
    survivor: str
    action: Literal["merge"]
    decided_at: str
    revision: str
    pending_apply: bool
    playlists: list[PlaylistRewriteOut]


class UndoRecordOut(BaseModel):
    cluster_id: int
    cluster_key: str
    survivor: str
    action: Literal["merge"]
    decided_at: str
    revision: str
    pending_apply: bool
    playlist_ids: list[str]


def _require_if_match(request: Request) -> str:
    if_match = request.headers.get("If-Match")
    if if_match is None:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            detail={
                "error": "precondition_required",
                "message": "POST /dedup/clusters/{cluster_id} writes require If-Match",
            },
        )
    return if_match


def _commit_store(**kwargs: Any) -> str:
    try:
        return commit_decision_store(**kwargs)
    except DecisionRevisionConflict as exc:
        raise conflict(exc.current_revision, exc.message) from exc
    except InvalidDecisionStoreError as exc:
        raise decision_store_error(str(exc)) from exc


@router.get(
    "/clusters",
    response_model=ClustersOut,
    responses=_GET_RESPONSES,
)
def get_dedup_clusters(
    response: Response,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008
) -> ClustersOut:
    store, revision = read_store_or_http()
    response.headers["ETag"] = revision
    db_path = dedup_db_path()
    if not db_path.is_file():
        return ClustersOut(
            clusters=[],
            revision=revision,
            note=(
                f"no dedup fingerprint database found at {db_path}; run "
                "`python -m apps.dedup.scan` then "
                "`python -m apps.dedup.find_clusters` first"
            ),
        )

    raw_clusters = read_raw_clusters(db_path)
    if raw_clusters is None:
        return ClustersOut(
            clusters=[],
            revision=revision,
            note=(
                "fingerprints exist but duplicate clusters are not materialized; "
                "run `python -m apps.dedup.find_clusters`"
            ),
        )

    stable_ids = sorted(
        {
            member_id
            for cluster in raw_clusters
            for member_id in cluster_member_ids(cluster)
        }
    )
    tracks = backend.get_tracks_bulk(stable_ids)
    cue_presence = bulk_cue_presence(stable_ids)
    empty_presence = CuePresence()

    clusters_out: list[ClusterOut] = []
    for cluster in raw_clusters:
        member_ids = cluster_member_ids(cluster)
        key = cluster_key(member_ids)
        members = [
            hydrate_member(
                tracks,
                stable_id=cluster["canonical_stable_id"],
                path=cluster["canonical_path"],
                is_canonical=True,
                similarity=None,
                presence=cue_presence.get(cluster["canonical_stable_id"], empty_presence),
                member_out_cls=MemberOut,
            )
        ]
        members.extend(
            hydrate_member(
                tracks,
                stable_id=alias["alias_stable_id"],
                path=alias["alias_path"],
                is_canonical=False,
                similarity=alias["similarity"],
                presence=cue_presence.get(alias["alias_stable_id"], empty_presence),
                member_out_cls=MemberOut,
            )
            for alias in cluster["aliases"]
        )
        decision = store.decisions.get(key)
        if decision is not None and decision.member_stable_ids != member_ids:
            raise decision_store_error(
                f"decision {key!r} does not match the current cluster members"
            )
        clusters_out.append(
            ClusterOut(
                cluster_id=cluster["cluster_id"],
                cluster_key=key,
                survivor_stable_id=cluster["canonical_stable_id"],
                rationale=cluster["rationale"],
                flagged_manual_review=cluster_needs_manual_review(cluster),
                members=members,
                decision=(
                    DecisionOut(
                        cluster_key=decision.cluster_key,
                        survivor=decision.survivor,
                        action=decision.action,
                        decided_at=decision.decided_at,
                        pending_apply=key not in store.applies,
                    )
                    if decision is not None
                    else None
                ),
            )
        )
    reject_duplicate_cluster_keys(clusters_out)
    return ClustersOut(clusters=clusters_out, revision=revision)


@router.post(
    "/clusters/{cluster_id}/decision",
    response_model=DecisionRecordOut,
    responses=_POST_RESPONSES,
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
)
def post_dedup_decision(
    cluster_id: int,
    body: DecisionIn,
    response: Response,
    if_match: str = Depends(_require_if_match),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008
) -> DecisionRecordOut:
    """Persist a pending review decision without applying a merge."""
    member_ids, current_cluster_key = load_cluster_for_write(cluster_id)
    require_identity(
        body_key=body.cluster_key,
        current_key=current_cluster_key,
        member_ids=member_ids,
        survivor=body.survivor,
        cluster_id=cluster_id,
        stale_message="cluster membership changed; refresh before recording a decision",
        read_store=read_store_or_http,
    )
    record = PersistedDecision(
        cluster_id=cluster_id,
        cluster_key=current_cluster_key,
        member_stable_ids=member_ids,
        survivor=body.survivor,
        action=body.action,
        decided_at=now_iso(),
    )
    revision = _commit_store(expected_revision=if_match, decision=record)
    response.headers["ETag"] = revision
    publish("library.changed", {"kind": "dedup", "ids": [str(cluster_id)]})
    return DecisionRecordOut(
        cluster_id=cluster_id,
        cluster_key=current_cluster_key,
        survivor=record.survivor,
        action=record.action,
        decided_at=record.decided_at,
        revision=revision,
        pending_apply=True,
    )


@router.post(
    "/clusters/{cluster_id}/apply",
    response_model=ApplyRecordOut,
    responses=_POST_RESPONSES,
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
)
def post_dedup_apply(
    cluster_id: int,
    body: ApplyIn,
    response: Response,
    if_match: str = Depends(_require_if_match),
    backend: StateBackend = Depends(get_write_state),  # noqa: B008
    store: PlaylistStore = Depends(get_playlist_store),  # noqa: B008
) -> ApplyRecordOut:
    """Rewrite OpenDJ playlist memberships so aliases point at the survivor."""
    member_ids, current_cluster_key = load_cluster_for_write(cluster_id)
    require_identity(
        body_key=body.cluster_key,
        current_key=current_cluster_key,
        member_ids=member_ids,
        survivor=body.survivor,
        cluster_id=cluster_id,
        stale_message="cluster membership changed; refresh before applying a merge",
        read_store=read_store_or_http,
    )
    if not body.confirm_cue_loss:
        cue_loss = cue_loss_report(member_ids, body.survivor)
        if cue_loss:
            raise cue_loss_confirmation_required(cue_loss)
    with decision_file_lock():
        snap, current_revision = read_store_or_http()
        if if_match != current_revision:
            raise conflict(
                current_revision,
                "If-Match does not match the current decision-store ETag",
            )
        record, apply_row, journal = prepare_merge_apply(
            snap=snap,
            current_revision=current_revision,
            current_cluster_key=current_cluster_key,
            cluster_id=cluster_id,
            survivor=body.survivor,
            member_ids=member_ids,
            backend=backend,
            store=store,
        )
        revision = persist_apply_journal(snap, record=record, apply_row=apply_row)

    response.headers["ETag"] = revision
    publish("library.changed", {"kind": "dedup", "ids": [str(cluster_id)]})
    playlists_out = [
        PlaylistRewriteOut(
            playlist_id=entry.playlist_id,
            name=playlist_name(backend, entry.playlist_id),
            before=list(entry.before),
            after=list(entry.after),
        )
        for entry in journal
    ]
    return ApplyRecordOut(
        cluster_id=cluster_id,
        cluster_key=current_cluster_key,
        survivor=body.survivor,
        action="merge",
        decided_at=record.decided_at,
        revision=revision,
        pending_apply=False,
        playlists=playlists_out,
    )


@router.post(
    "/clusters/{cluster_id}/undo",
    response_model=UndoRecordOut,
    responses=_POST_RESPONSES,
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
)
def post_dedup_undo(
    cluster_id: int,
    body: ApplyIn,
    response: Response,
    if_match: str = Depends(_require_if_match),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008
    store: PlaylistStore = Depends(get_playlist_store),  # noqa: B008
) -> UndoRecordOut:
    """Restore playlist memberships from the apply journal for this cluster."""
    member_ids, current_cluster_key = load_cluster_for_write(cluster_id)
    require_identity(
        body_key=body.cluster_key,
        current_key=current_cluster_key,
        member_ids=member_ids,
        survivor=body.survivor,
        cluster_id=cluster_id,
        stale_message="cluster membership changed; refresh before undoing a merge",
        read_store=read_store_or_http,
    )
    with decision_file_lock():
        snap, current_revision = read_store_or_http()
        if if_match != current_revision:
            raise conflict(
                current_revision,
                "If-Match does not match the current decision-store ETag",
            )
        apply_row = prepare_merge_undo(
            snap=snap,
            current_revision=current_revision,
            current_cluster_key=current_cluster_key,
            cluster_id=cluster_id,
            body_key=body.cluster_key,
            survivor=body.survivor,
            store=store,
        )
        revision = drop_apply_journal(snap, current_cluster_key)
        decision = snap.decisions.get(current_cluster_key)

    response.headers["ETag"] = revision
    publish("library.changed", {"kind": "dedup", "ids": [str(cluster_id)]})
    decided_at = decision.decided_at if decision is not None else now_iso()
    return UndoRecordOut(
        cluster_id=cluster_id,
        cluster_key=current_cluster_key,
        survivor=body.survivor,
        action="merge",
        decided_at=decided_at,
        revision=revision,
        pending_apply=True,
        playlist_ids=[entry.playlist_id for entry in apply_row.playlists],
    )


__all__ = ["DECISIONS_FILE", "dedup_paths", "router"]
