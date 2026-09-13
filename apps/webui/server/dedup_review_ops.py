"""Cluster hydration and OpenDJ membership apply/undo for the review routes."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from apps.dedup.find_clusters import (
    DEFAULT_DURATION_DELTA_S,
    DEFAULT_MAX_CLUSTER,
)
from apps.shared import fs_residency
from apps.shared import paths as dedup_paths

from .backend import BackendError, ConflictError, NotFoundError, StateBackend
from .dedup_decisions import (
    DecisionStore,
    PersistedApply,
    PersistedApplyPlaylist,
    PersistedDecision,
    dump_decision_store_atomic,
    read_decision_snapshot,
)
from .dedup_playlist_rewrite import playlists_to_rewrite, rewrite_items
from .playlist_store import PlaylistStore


def http_error(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"error": error, "message": message})


def decision_store_error(message: str) -> HTTPException:
    return http_error(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "invalid_decision_store",
        message,
    )


def conflict(current_revision: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "conflict",
            "message": message,
            "revision": current_revision,
        },
        headers={"ETag": current_revision},
    )


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def dedup_db_path() -> Path:
    return dedup_paths.DEDUP_FALLBACK_DB


def read_raw_clusters(db_path: Path) -> list[dict[str, Any]] | None:
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


def cluster_member_ids(cluster: dict[str, Any]) -> list[str]:
    member_ids = [cluster["canonical_stable_id"]]
    member_ids.extend(alias["alias_stable_id"] for alias in cluster["aliases"])
    if any(not isinstance(member_id, str) or not member_id for member_id in member_ids):
        raise http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "invalid_cluster_identity",
            f"cluster {cluster['cluster_id']} has a missing stable id",
        )
    unique_ids = sorted(set(member_ids))
    if len(unique_ids) != len(member_ids):
        raise http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "invalid_cluster_identity",
            f"cluster {cluster['cluster_id']} has duplicate stable ids",
        )
    return unique_ids


def cluster_key(member_stable_ids: list[str]) -> str:
    canonical_members = json.dumps(
        member_stable_ids,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical_members).hexdigest()}"


def cluster_needs_manual_review(cluster: dict[str, Any]) -> bool:
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


def hydrate_member(
    tracks: dict[str, Any],
    *,
    stable_id: str,
    path: str,
    is_canonical: bool,
    similarity: float | None,
    presence: Any,
    member_out_cls: Any,
) -> Any:
    track = tracks.get(stable_id)
    return member_out_cls(
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
        cue_count=presence.cue_count,
        hot_cue_count=presence.hot_cue_count,
        loop_count=presence.loop_count,
        has_beatgrid=presence.has_beatgrid,
        cue_positions_ms=list(presence.cue_positions_ms),
    )


def find_raw_cluster(
    clusters: list[dict[str, Any]],
    cluster_id: int,
) -> dict[str, Any]:
    for cluster in clusters:
        if cluster["cluster_id"] == cluster_id:
            return cluster
    raise http_error(
        status.HTTP_404_NOT_FOUND,
        "not_found",
        f"unknown cluster_id: {cluster_id}",
    )


def reject_duplicate_cluster_keys(clusters: list[Any]) -> None:
    seen: dict[str, int] = {}
    for cluster in clusters:
        previous = seen.get(cluster.cluster_key)
        if previous is not None:
            raise http_error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "invalid_cluster_identity",
                f"duplicate cluster_key {cluster.cluster_key} "
                f"(cluster_id {previous} and {cluster.cluster_id})",
            )
        seen[cluster.cluster_key] = cluster.cluster_id


def load_cluster_for_write(cluster_id: int) -> tuple[list[str], str]:
    db_path = dedup_db_path()
    if not db_path.is_file():
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            f"no dedup fingerprint database found at {db_path}",
        )
    raw_clusters = read_raw_clusters(db_path)
    if raw_clusters is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            "duplicate clusters are not materialized; run apps.dedup.find_clusters",
        )
    cluster = find_raw_cluster(raw_clusters, cluster_id)
    member_ids = cluster_member_ids(cluster)
    return member_ids, cluster_key(member_ids)


def require_identity(
    *,
    body_key: str,
    current_key: str,
    member_ids: list[str],
    survivor: str,
    cluster_id: int,
    stale_message: str,
    read_store: Any,
) -> None:
    if body_key != current_key:
        _, current_revision = read_store()
        raise conflict(current_revision, stale_message)
    if survivor not in member_ids:
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_survivor",
            f"{survivor!r} is not a member of cluster {cluster_id}",
        )


def playlist_name(backend: StateBackend, playlist_id: str) -> str:
    try:
        return backend.get_playlist(playlist_id).name
    except NotFoundError:
        return playlist_id


def rewrite_playlists(
    backend: StateBackend,
    store: PlaylistStore,
    alias_ids: set[str],
    survivor: str,
) -> list[PersistedApplyPlaylist]:
    targets = playlists_to_rewrite(backend.list_playlists(), alias_ids)
    applied: list[PersistedApplyPlaylist] = []
    try:
        for playlist in targets:
            row = store.get_playlist_row(playlist.playlist_id)
            before = list(row.items)
            after = rewrite_items(before, alias_ids, survivor)
            store.replace_memberships(
                playlist.playlist_id, after, expected_etag=row.etag,
            )
            applied.append(
                PersistedApplyPlaylist(
                    playlist_id=playlist.playlist_id,
                    before=before,
                    after=after,
                )
            )
    except (BackendError, ConflictError, NotFoundError):
        _compensate_memberships(store, reversed(applied), use_before=True)
        raise
    return applied


def _journal_after_mismatch(
    current_revision: str,
) -> HTTPException:
    return conflict(
        current_revision,
        "playlist membership changed since apply; refresh before undo",
    )


def _compensate_memberships(
    store: PlaylistStore,
    entries: Any,
    *,
    use_before: bool,
) -> None:
    for entry in entries:
        try:
            current = store.get_playlist_row(entry.playlist_id)
            target = entry.before if use_before else entry.after
            store.replace_memberships(
                entry.playlist_id, target, expected_etag=current.etag,
            )
        except (BackendError, ConflictError, NotFoundError):
            pass


def restore_playlists(
    store: PlaylistStore,
    entries: list[PersistedApplyPlaylist],
    *,
    current_revision: str,
) -> None:
    restored: list[PersistedApplyPlaylist] = []
    try:
        for entry in entries:
            row = store.get_playlist_row(entry.playlist_id)
            if list(row.items) != list(entry.after):
                raise _journal_after_mismatch(current_revision)
            store.replace_memberships(
                entry.playlist_id, entry.before, expected_etag=row.etag,
            )
            restored.append(entry)
    except (BackendError, ConflictError, HTTPException, NotFoundError):
        _compensate_memberships(store, reversed(restored), use_before=False)
        raise


def persist_apply_journal(
    snap: DecisionStore,
    *,
    record: PersistedDecision,
    apply_row: PersistedApply,
) -> str:
    decisions = dict(snap.decisions)
    applies = dict(snap.applies)
    decisions[record.cluster_key] = record
    applies[apply_row.cluster_key] = apply_row
    return dump_decision_store_atomic(
        DecisionStore(schema_version=2, decisions=decisions, applies=applies)
    )


def drop_apply_journal(snap: DecisionStore, cluster_key_value: str) -> str:
    applies = dict(snap.applies)
    applies.pop(cluster_key_value, None)
    return dump_decision_store_atomic(
        DecisionStore(
            schema_version=2, decisions=dict(snap.decisions), applies=applies,
        )
    )


def prepare_merge_apply(
    *,
    snap: DecisionStore,
    current_revision: str,
    current_cluster_key: str,
    cluster_id: int,
    survivor: str,
    member_ids: list[str],
    backend: StateBackend,
    store: PlaylistStore,
) -> tuple[PersistedDecision, PersistedApply, list[PersistedApplyPlaylist]]:
    existing_decision = snap.decisions.get(current_cluster_key)
    if existing_decision is not None and existing_decision.action != "merge":
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "decision_is_not_merge",
            f"cluster {cluster_id} is {existing_decision.action}, not merge",
        )
    existing_apply = snap.applies.get(current_cluster_key)
    if existing_apply is not None and existing_apply.survivor != survivor:
        raise conflict(
            current_revision,
            "apply journal survivor does not match the request",
        )
    alias_ids = sorted(sid for sid in member_ids if sid != survivor)
    try:
        rewritten = rewrite_playlists(backend, store, set(alias_ids), survivor)
    except ConflictError as exc:
        raise conflict(
            current_revision,
            "playlist membership changed during apply; compensated",
        ) from exc
    journal = rewritten
    if not journal and existing_apply is not None:
        journal = list(existing_apply.playlists)
    decided_at = (
        existing_decision.decided_at if existing_decision is not None else now_iso()
    )
    record = PersistedDecision(
        cluster_id=cluster_id,
        cluster_key=current_cluster_key,
        member_stable_ids=member_ids,
        survivor=survivor,
        action="merge",
        decided_at=decided_at,
    )
    apply_row = PersistedApply(
        cluster_key=current_cluster_key,
        survivor=survivor,
        alias_stable_ids=alias_ids,
        applied_at=now_iso(),
        playlists=journal,
    )
    return record, apply_row, journal


def prepare_merge_undo(
    *,
    snap: DecisionStore,
    current_revision: str,
    current_cluster_key: str,
    cluster_id: int,
    body_key: str,
    survivor: str,
    store: PlaylistStore,
) -> PersistedApply:
    apply_row = snap.applies.get(current_cluster_key)
    if apply_row is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            "not_found",
            f"no apply journal for cluster {cluster_id}",
        )
    if apply_row.survivor != survivor or apply_row.cluster_key != body_key:
        raise conflict(
            current_revision,
            "apply journal does not match the requested survivor or cluster_key",
        )
    try:
        restore_playlists(
            store, list(apply_row.playlists), current_revision=current_revision,
        )
    except ConflictError as exc:
        raise conflict(
            current_revision,
            "playlist membership changed during undo; compensated",
        ) from exc
    return apply_row


def read_store_or_http() -> tuple[DecisionStore, str]:
    from .dedup_decisions import InvalidDecisionStoreError

    try:
        return read_decision_snapshot()
    except InvalidDecisionStoreError as exc:
        raise decision_store_error(str(exc)) from exc
