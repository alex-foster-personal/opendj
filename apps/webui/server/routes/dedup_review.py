"""Duplicate-cluster review routes with identity-bound, CAS-guarded decisions.

``GET /dedup/clusters`` exposes the derived duplicate clusters and the exact
decision-store revision. ``POST /dedup/clusters/{cluster_id}/decision`` only
records a pending decision. It never invokes ``apps.dedup.apply``, rewrites a
playlist, changes the duplicate database, or deletes an audio file.

The numeric SQLite cluster id is display-only. Decisions are keyed by a stable
SHA-256 digest of the cluster member stable ids, so rebuilding the derived
tables cannot attach an old decision to unrelated tracks that reused an id.
Every write requires ``If-Match`` and is a locked read-modify-replace cycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from apps.shared import fs_residency

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, ValidationError

from apps.dedup.find_clusters import (
    DEFAULT_DURATION_DELTA_S,
    DEFAULT_MAX_CLUSTER,
)
from apps.shared import paths as dedup_paths
from apps.shared.events import publish

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state

router = APIRouter(prefix="/dedup", tags=["dedup-review"])

DECISIONS_FILE: Path = dedup_paths.DEDUP_DIR / "review-decisions.json"
_WRITE_LOCK = threading.Lock()
_DECISION_SCHEMA_VERSION = 1

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


# ----- API and persisted models ----------------------------------------------


class MemberOut(BaseModel):
    stable_id: str
    path: str
    is_canonical: bool
    similarity: Optional[float]
    title: Optional[str]
    artist: Optional[str]
    bpm: Optional[float]
    key: Optional[str]
    duration_ms: Optional[int]
    rating: Optional[int]
    file_exists: bool


class DecisionOut(BaseModel):
    cluster_key: str
    survivor: str
    action: ActionLiteral
    decided_at: str


class ClusterOut(BaseModel):
    cluster_id: int
    cluster_key: str
    survivor_stable_id: str
    rationale: Optional[str]
    flagged_manual_review: bool
    members: list[MemberOut]
    decision: Optional[DecisionOut] = None


class ClustersOut(BaseModel):
    clusters: list[ClusterOut]
    revision: str
    note: Optional[str] = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_key: str
    survivor: str
    action: ActionLiteral


class DecisionRecordOut(BaseModel):
    cluster_id: int
    cluster_key: str
    survivor: str
    action: ActionLiteral
    decided_at: str
    revision: str
    pending_apply: bool = True


class PersistedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_id: int
    cluster_key: str
    member_stable_ids: list[str]
    survivor: str
    action: ActionLiteral
    decided_at: str


class DecisionStore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    decisions: dict[str, PersistedDecision]


# ----- persistence ------------------------------------------------------------


def _http_error(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"error": error, "message": message})


def _decision_store_error(message: str) -> HTTPException:
    return _http_error(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "invalid_decision_store",
        message,
    )


def _empty_decision_store() -> DecisionStore:
    return DecisionStore(
        schema_version=_DECISION_SCHEMA_VERSION,
        decisions={},
    )


def _encode_decision_store(store: DecisionStore) -> bytes:
    payload = json.dumps(
        store.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
    )
    return f"{payload}\n".encode("utf-8")


def _decision_revision(payload: bytes) -> str:
    return f'"{hashlib.sha256(payload).hexdigest()}"'


def _validate_decision_store(store: DecisionStore) -> None:
    for cluster_key, decision in store.decisions.items():
        expected_members = sorted(set(decision.member_stable_ids))
        if cluster_key != decision.cluster_key:
            raise _decision_store_error(
                f"decision key {cluster_key!r} does not match its cluster_key"
            )
        if not expected_members or expected_members != decision.member_stable_ids:
            raise _decision_store_error(
                f"decision {cluster_key!r} has empty, duplicate, or unsorted members"
            )
        if decision.survivor not in expected_members:
            raise _decision_store_error(
                f"decision {cluster_key!r} names a survivor outside its members"
            )


def _read_decision_snapshot() -> tuple[DecisionStore, str]:
    if not DECISIONS_FILE.is_file():
        store = _empty_decision_store()
        payload = _encode_decision_store(store)
        return store, _decision_revision(payload)
    try:
        payload = DECISIONS_FILE.read_bytes()
    except OSError as exc:
        raise _decision_store_error(
            f"cannot read {DECISIONS_FILE}: {exc}"
        ) from exc
    try:
        store = DecisionStore.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        raise _decision_store_error(
            f"{DECISIONS_FILE} is not a valid versioned decision store: {exc}"
        ) from exc
    _validate_decision_store(store)
    return store, _decision_revision(payload)


def _dump_decision_store_atomic(store: DecisionStore) -> str:
    payload = _encode_decision_store(store)
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(
        dir=str(DECISIONS_FILE.parent),
        prefix=".review-decisions.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, DECISIONS_FILE)
        if os.name != "nt":
            directory_descriptor = os.open(DECISIONS_FILE.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise
    return _decision_revision(payload)


@contextmanager
def _decision_file_lock() -> Iterator[None]:
    """Serialize the complete read-check-write cycle across processes."""
    with _WRITE_LOCK:
        DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_path = DECISIONS_FILE.with_name(f"{DECISIONS_FILE.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            if os.name == "nt":
                lock_handle.seek(0, os.SEEK_END)
                if lock_handle.tell() == 0:
                    lock_handle.write(b"\0")
                    lock_handle.flush()
                    os.fsync(lock_handle.fileno())
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _conflict(current_revision: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "conflict",
            "message": message,
            "revision": current_revision,
        },
        headers={"ETag": current_revision},
    )


def _persist_decision(
    record: PersistedDecision,
    expected_revision: str,
) -> str:
    with _decision_file_lock():
        store, current_revision = _read_decision_snapshot()
        if expected_revision != current_revision:
            raise _conflict(
                current_revision,
                "If-Match does not match the current decision-store ETag",
            )
        decisions = dict(store.decisions)
        decisions[record.cluster_key] = record
        return _dump_decision_store_atomic(
            DecisionStore(
                schema_version=_DECISION_SCHEMA_VERSION,
                decisions=decisions,
            )
        )


# ----- cluster database and hydration ----------------------------------------


def _dedup_db_path() -> Path:
    return dedup_paths.DEDUP_FALLBACK_DB


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_raw_clusters(db_path: Path) -> Optional[list[dict[str, Any]]]:
    """Return None when fingerprinting exists but clustering has not run."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"duplicate_clusters", "track_aliases"} <= table_names:
            return None
        cluster_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(duplicate_clusters)")
        }
        flag_expression = (
            "flagged_manual_review"
            if "flagged_manual_review" in cluster_columns
            else "0 AS flagged_manual_review"
        )
        canonical_duration_expression = (
            "(SELECT duration FROM fingerprints "
            "WHERE path = duplicate_clusters.canonical_path) AS canonical_duration"
            if "fingerprints" in table_names
            else "NULL AS canonical_duration"
        )
        cluster_rows = connection.execute(
            "SELECT cluster_id, canonical_stable_id, canonical_path, rationale, "
            f"{flag_expression}, {canonical_duration_expression} "
            "FROM duplicate_clusters ORDER BY cluster_id"
        ).fetchall()
        clusters: list[dict[str, Any]] = []
        for cluster_row in cluster_rows:
            alias_duration_expression = (
                "(SELECT duration FROM fingerprints "
                "WHERE path = track_aliases.alias_path) AS alias_duration"
                if "fingerprints" in table_names
                else "NULL AS alias_duration"
            )
            alias_rows = connection.execute(
                "SELECT alias_stable_id, alias_path, similarity, "
                f"{alias_duration_expression} "
                "FROM track_aliases WHERE cluster_id = ? ORDER BY alias_path",
                (cluster_row["cluster_id"],),
            ).fetchall()
            cluster = dict(cluster_row)
            cluster["aliases"] = [dict(alias) for alias in alias_rows]
            clusters.append(cluster)
        return clusters
    finally:
        connection.close()


def _cluster_member_ids(cluster: dict[str, Any]) -> list[str]:
    member_ids = [cluster["canonical_stable_id"]]
    member_ids.extend(alias["alias_stable_id"] for alias in cluster["aliases"])
    if any(not isinstance(member_id, str) or not member_id for member_id in member_ids):
        raise _http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "invalid_cluster_identity",
            f"cluster {cluster['cluster_id']} has a missing stable id",
        )
    unique_ids = sorted(set(member_ids))
    if len(unique_ids) != len(member_ids):
        raise _http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "invalid_cluster_identity",
            f"cluster {cluster['cluster_id']} has duplicate stable ids",
        )
    return unique_ids


def _cluster_key(member_stable_ids: list[str]) -> str:
    canonical_members = json.dumps(
        member_stable_ids,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical_members).hexdigest()}"


def _cluster_needs_manual_review(cluster: dict[str, Any]) -> bool:
    if bool(cluster["flagged_manual_review"]):
        return True
    if len(cluster["aliases"]) + 1 > DEFAULT_MAX_CLUSTER:
        return True
    canonical_duration = cluster["canonical_duration"]
    if canonical_duration is None:
        return False
    return any(
        alias["alias_duration"] is not None
        and abs(float(canonical_duration) - float(alias["alias_duration"]))
        > DEFAULT_DURATION_DELTA_S
        for alias in cluster["aliases"]
    )


def _hydrate_member(
    tracks: dict[str, Any],
    *,
    stable_id: str,
    path: str,
    is_canonical: bool,
    similarity: Optional[float],
) -> MemberOut:
    track = tracks.get(stable_id)
    return MemberOut(
        stable_id=stable_id,
        path=path,
        is_canonical=is_canonical,
        similarity=similarity,
        title=track.title if track else None,
        artist=track.artist if track else None,
        bpm=track.bpm if track else None,
        key=track.key if track else None,
        duration_ms=track.duration_ms if track else None,
        rating=track.rating if track else None,
        file_exists=bool(path) and fs_residency.is_materialised(Path(path)),
    )


def _find_raw_cluster(
    clusters: list[dict[str, Any]],
    cluster_id: int,
) -> dict[str, Any]:
    for cluster in clusters:
        if cluster["cluster_id"] == cluster_id:
            return cluster
    raise _http_error(
        status.HTTP_404_NOT_FOUND,
        "not_found",
        f"unknown cluster_id: {cluster_id}",
    )


def _require_if_match(request: Request) -> str:
    if_match = request.headers.get("If-Match")
    if if_match is None:
        raise _http_error(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "precondition_required",
            "POST /dedup/clusters/{cluster_id}/decision requires If-Match",
        )
    return if_match


# ----- routes -----------------------------------------------------------------


@router.get(
    "/clusters",
    response_model=ClustersOut,
    responses=_GET_RESPONSES,
)
def get_dedup_clusters(
    response: Response,
    backend: StateBackend = Depends(get_read_state),
) -> ClustersOut:
    store, revision = _read_decision_snapshot()
    response.headers["ETag"] = revision
    db_path = _dedup_db_path()
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

    raw_clusters = _read_raw_clusters(db_path)
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
            for member_id in _cluster_member_ids(cluster)
        }
    )
    tracks = backend.get_tracks_bulk(stable_ids)

    clusters_out: list[ClusterOut] = []
    for cluster in raw_clusters:
        member_ids = _cluster_member_ids(cluster)
        cluster_key = _cluster_key(member_ids)
        members = [
            _hydrate_member(
                tracks,
                stable_id=cluster["canonical_stable_id"],
                path=cluster["canonical_path"],
                is_canonical=True,
                similarity=None,
            )
        ]
        members.extend(
            _hydrate_member(
                tracks,
                stable_id=alias["alias_stable_id"],
                path=alias["alias_path"],
                is_canonical=False,
                similarity=alias["similarity"],
            )
            for alias in cluster["aliases"]
        )
        decision = store.decisions.get(cluster_key)
        if decision is not None and decision.member_stable_ids != member_ids:
            raise _decision_store_error(
                f"decision {cluster_key!r} does not match the current cluster members"
            )
        clusters_out.append(
            ClusterOut(
                cluster_id=cluster["cluster_id"],
                cluster_key=cluster_key,
                survivor_stable_id=cluster["canonical_stable_id"],
                rationale=cluster["rationale"],
                flagged_manual_review=_cluster_needs_manual_review(cluster),
                members=members,
                decision=(
                    DecisionOut(
                        cluster_key=decision.cluster_key,
                        survivor=decision.survivor,
                        action=decision.action,
                        decided_at=decision.decided_at,
                    )
                    if decision is not None
                    else None
                ),
            )
        )
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
    _backend: StateBackend = Depends(get_write_state),
) -> DecisionRecordOut:
    """Persist a pending review decision without applying a merge."""
    db_path = _dedup_db_path()
    if not db_path.is_file():
        raise _http_error(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            f"no dedup fingerprint database found at {db_path}",
        )
    raw_clusters = _read_raw_clusters(db_path)
    if raw_clusters is None:
        raise _http_error(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            "duplicate clusters are not materialized; run apps.dedup.find_clusters",
        )
    cluster = _find_raw_cluster(raw_clusters, cluster_id)
    member_ids = _cluster_member_ids(cluster)
    current_cluster_key = _cluster_key(member_ids)
    if body.cluster_key != current_cluster_key:
        _, current_revision = _read_decision_snapshot()
        raise _conflict(
            current_revision,
            "cluster membership changed; refresh before recording a decision",
        )
    if body.survivor not in member_ids:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_survivor",
            f"{body.survivor!r} is not a member of cluster {cluster_id}",
        )

    record = PersistedDecision(
        cluster_id=cluster_id,
        cluster_key=current_cluster_key,
        member_stable_ids=member_ids,
        survivor=body.survivor,
        action=body.action,
        decided_at=_now_iso(),
    )
    revision = _persist_decision(record, if_match)
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


__all__ = ["router"]
