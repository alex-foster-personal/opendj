"""Dedup review routes (duplicate-review-merge) -- CAT-05 dedup queue surface.

Read-only cluster surface over the Phase 7 dedup tables
(``duplicate_clusters`` + ``track_aliases`` in ``apps.dedup.schema``),
hydrated with track rows from the :class:`StateBackend` so the review UI
can render a side-by-side comparison per cluster.

Endpoints:

  * ``GET  /dedup/clusters`` -- every cluster + hydrated members + the
    survivor proposal already computed by ``apps.dedup.find_clusters``.
    Returns an empty list with a ``note`` when no fingerprint database
    exists yet (nothing has been scanned/clustered), never a 500.
  * ``POST /dedup/clusters/{cluster_id}/decision`` -- stores ONE decision
    record ``{survivor, action}`` in a sidecar JSON file
    (``data/dedup/review-decisions.json``). This is intentionally NOT a
    merge-execution endpoint: no playlist references are rewritten here.
    Real rewriting is ``apps.dedup.apply`` (local-verified follow-up, see
    PARITY-TODO.md). Every stored record is pending-apply.

The integrator wires ``router`` into ``create_app()`` under ``/api/v1``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from apps.shared import paths as dedup_paths
from apps.dedup.find_clusters import DEFAULT_MAX_CLUSTER

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state

router = APIRouter(prefix="/dedup", tags=["dedup-review"])

DECISIONS_FILE: Path = dedup_paths.DEDUP_DIR / "review-decisions.json"
_WRITE_LOCK = threading.Lock()

ActionLiteral = Literal["merge", "keep-all", "skip"]


# ----- pydantic models (inline per router convention) ------------------------

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
    survivor: str
    action: ActionLiteral
    decided_at: str


class ClusterOut(BaseModel):
    cluster_id: int
    survivor_stable_id: str
    rationale: Optional[str]
    flagged_manual_review: bool
    members: list[MemberOut]
    decision: Optional[DecisionOut] = None


class ClustersOut(BaseModel):
    clusters: list[ClusterOut]
    note: Optional[str] = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    survivor: str
    action: ActionLiteral


class DecisionRecordOut(BaseModel):
    cluster_id: int
    survivor: str
    action: ActionLiteral
    decided_at: str
    pending_apply: bool = True


# ----- helpers ----------------------------------------------------------------

def _http_error(code: int, error: str, message: str) -> HTTPException:
    """Match the repo-wide {detail: {error, message}} error body shape."""
    return HTTPException(code, detail={"error": error, "message": message})


def _dedup_db_path() -> Path:
    return dedup_paths.DEDUP_FALLBACK_DB


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _load_decisions() -> dict[str, dict[str, Any]]:
    if not DECISIONS_FILE.is_file():
        return {}
    try:
        data = json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _dump_decisions_atomic(decisions: dict[str, dict[str, Any]]) -> None:
    """Atomic write: tmp file in the same dir + os.replace (progress.py convention)."""
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(DECISIONS_FILE.parent), prefix=".review-decisions.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(decisions, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, DECISIONS_FILE)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _read_raw_clusters(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cluster_rows = conn.execute(
            "SELECT cluster_id, canonical_stable_id, canonical_path, rationale "
            "FROM duplicate_clusters ORDER BY cluster_id"
        ).fetchall()
        clusters: list[dict[str, Any]] = []
        for c in cluster_rows:
            alias_rows = conn.execute(
                "SELECT alias_stable_id, alias_path, similarity "
                "FROM track_aliases WHERE cluster_id = ? ORDER BY alias_path",
                (c["cluster_id"],),
            ).fetchall()
            clusters.append({
                "cluster_id": c["cluster_id"],
                "canonical_stable_id": c["canonical_stable_id"],
                "canonical_path": c["canonical_path"],
                "rationale": c["rationale"],
                "aliases": [dict(a) for a in alias_rows],
            })
        return clusters
    finally:
        conn.close()


def _hydrate_member(
    tracks: dict[str, Any], *, stable_id: str, path: str,
    is_canonical: bool, similarity: Optional[float],
) -> MemberOut:
    track = tracks.get(stable_id)
    file_exists = bool(path) and Path(path).is_file()
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
        file_exists=file_exists,
    )


def _find_raw_cluster(clusters: list[dict[str, Any]], cluster_id: int) -> dict[str, Any]:
    for c in clusters:
        if c["cluster_id"] == cluster_id:
            return c
    raise _http_error(
        status.HTTP_404_NOT_FOUND, "not_found",
        f"unknown cluster_id: {cluster_id}",
    )


# ----- routes -----------------------------------------------------------------

@router.get("/clusters", response_model=ClustersOut)
def get_dedup_clusters(backend: StateBackend = Depends(get_read_state)) -> ClustersOut:
    """Every cluster with hydrated members + the survivor proposal.

    Skips cleanly (200, empty list, explanatory note) when no fingerprint
    database has been built yet -- never a 500.
    """
    db_path = _dedup_db_path()
    if not db_path.is_file():
        return ClustersOut(
            clusters=[],
            note=(
                f"no dedup fingerprint database found at {db_path}; run "
                "`python -m apps.dedup.scan` then "
                "`python -m apps.dedup.find_clusters` first"
            ),
        )

    raw_clusters = _read_raw_clusters(db_path)
    decisions = _load_decisions()

    stable_ids: set[str] = set()
    for c in raw_clusters:
        if c["canonical_stable_id"]:
            stable_ids.add(c["canonical_stable_id"])
        for a in c["aliases"]:
            if a["alias_stable_id"]:
                stable_ids.add(a["alias_stable_id"])
    tracks = backend.get_tracks_bulk(sorted(stable_ids))

    clusters_out: list[ClusterOut] = []
    for c in raw_clusters:
        members = [
            _hydrate_member(
                tracks, stable_id=c["canonical_stable_id"],
                path=c["canonical_path"], is_canonical=True, similarity=None,
            )
        ]
        for a in c["aliases"]:
            members.append(_hydrate_member(
                tracks, stable_id=a["alias_stable_id"], path=a["alias_path"],
                is_canonical=False, similarity=a["similarity"],
            ))
        decision_raw = decisions.get(str(c["cluster_id"]))
        clusters_out.append(ClusterOut(
            cluster_id=c["cluster_id"],
            survivor_stable_id=c["canonical_stable_id"],
            rationale=c["rationale"],
            # Mirrors find_clusters._pick_canonical's max-cluster-size flag;
            # the per-alias duration-delta flag lives only in manual-review.csv.
            flagged_manual_review=len(members) > DEFAULT_MAX_CLUSTER,
            members=members,
            decision=DecisionOut(**decision_raw) if decision_raw else None,
        ))
    return ClustersOut(clusters=clusters_out)


@router.post(
    "/clusters/{cluster_id}/decision",
    response_model=DecisionRecordOut,
)
def post_dedup_decision(
    cluster_id: int,
    body: DecisionIn,
    _backend: StateBackend = Depends(get_write_state),
) -> DecisionRecordOut:
    """Store a decision record for one cluster. Pending-apply only.

    Never rewrites playlist references or touches any DB but the sidecar
    ``review-decisions.json`` file. Applying the merge is a local-verified
    follow-up (see PARITY-TODO.md).
    """
    db_path = _dedup_db_path()
    if not db_path.is_file():
        raise _http_error(
            status.HTTP_404_NOT_FOUND, "not_found",
            f"no dedup fingerprint database found at {db_path}",
        )
    raw_clusters = _read_raw_clusters(db_path)
    cluster = _find_raw_cluster(raw_clusters, cluster_id)

    member_ids = {cluster["canonical_stable_id"]} | {
        a["alias_stable_id"] for a in cluster["aliases"]
    }
    if body.survivor not in member_ids:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_survivor",
            f"{body.survivor!r} is not a member of cluster {cluster_id}",
        )

    record = {
        "survivor": body.survivor,
        "action": body.action,
        "decided_at": _now_iso(),
    }
    with _WRITE_LOCK:
        decisions = _load_decisions()
        decisions[str(cluster_id)] = record
        _dump_decisions_atomic(decisions)

    return DecisionRecordOut(cluster_id=cluster_id, **record, pending_apply=True)


__all__ = ["router"]
