"""Tag-only duplicate candidate clusters (artist, title, duration within tolerance)."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.dedup.contract import DEFAULT_MAX_CLUSTER
from apps.mik.duplicates import BASIS_METADATA, artist_title_duration_groups
from apps.shared import paths
from apps.shared.state.db import open_ro

from . import find_clusters
from . import schema as dedup_schema

TAG_RATIONALE = BASIS_METADATA
TAG_SIMILARITY = 0.0
TAG_BASIS = "tags"
MANUAL_REASON = TAG_RATIONALE


@dataclass(frozen=True, slots=True)
class TagClusterStats:
    written: int
    precision: float | None


def _member_rows(conn: sqlite3.Connection, stable_ids: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(stable_ids))
    query = (
        f"SELECT stable_id, file_path, title, artists_json, duration_ms FROM tracks "
        f"WHERE stable_id IN ({placeholders}) AND deleted_at IS NULL"
    )
    rows = conn.execute(query, stable_ids).fetchall()
    by_id = {
        sid: {
            "stable_id": sid,
            "path": fp or "",
            "title": title,
            "artists_json": artists_json,
            "duration_ms": duration_ms,
        }
        for sid, fp, title, artists_json, duration_ms in rows
    }
    return [by_id[sid] for sid in stable_ids if sid in by_id]


def _fingerprint_clustered_ids(dedup_conn: sqlite3.Connection) -> set[str]:
    ids: set[str] = set()
    for row in dedup_conn.execute(
        "SELECT canonical_stable_id FROM duplicate_clusters WHERE COALESCE(basis, 'fingerprint') != ?",
        (TAG_BASIS,),
    ):
        ids.add(str(row[0]))
    for row in dedup_conn.execute(
        "SELECT alias_stable_id FROM track_aliases "
        "JOIN duplicate_clusters USING (cluster_id) "
        "WHERE COALESCE(duplicate_clusters.basis, 'fingerprint') != ?",
        (TAG_BASIS,),
    ):
        ids.add(str(row[0]))
    return ids


def _pick_canonical_tag(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Longest duration, then lexicographically smallest stable_id."""
    with_duration = [m for m in members if m.get("duration_ms") is not None]
    pool = with_duration or members
    best = max(pool, key=lambda m: (int(m["duration_ms"] or 0), -len(str(m["path"]))))
    tied = [m for m in pool if int(m.get("duration_ms") or 0) == int(best.get("duration_ms") or 0)]
    tied.sort(key=lambda m: str(m["stable_id"]))
    return tied[0]


def run_tag_clusters(
    *,
    state_db: Path,
    db_path: Path,
    manual_review_csv: Path | None = None,
    clusters_csv: Path | None = None,
) -> TagClusterStats:
    """Append tag-based clusters; never re-groups fingerprint-clustered members."""
    use_manual = manual_review_csv if manual_review_csv is not None else paths.DEDUP_MANUAL_REVIEW_CSV
    use_csv = clusters_csv if clusters_csv is not None else paths.DEDUP_CLUSTERS_CSV
    state_conn = open_ro(state_db)
    dedup_conn = dedup_schema.ensure_schema(db_path)
    try:
        clustered = _fingerprint_clustered_ids(dedup_conn)
        groups = [
            g
            for g in artist_title_duration_groups(state_conn)
            if 2 <= len(g) <= DEFAULT_MAX_CLUSTER and not any(sid in clustered for sid in g)
        ]
        written = 0
        use_manual.parent.mkdir(parents=True, exist_ok=True)
        use_csv.parent.mkdir(parents=True, exist_ok=True)
        append_manual = use_manual.exists() and use_manual.stat().st_size > 0
        append_clusters = use_csv.exists() and use_csv.stat().st_size > 0
        with (
            open(use_manual, "a", newline="", encoding="utf-8") as manual_f,
            open(use_csv, "a", newline="", encoding="utf-8") as csv_f,
        ):
            manual_writer = csv.writer(manual_f)
            cluster_writer = csv.writer(csv_f)
            if not append_manual:
                manual_writer.writerow(find_clusters.MANUAL_REVIEW_CSV_HEADER)
            if not append_clusters:
                cluster_writer.writerow(find_clusters.CLUSTERS_CSV_HEADER)

            for stable_ids in groups:
                members = _member_rows(state_conn, list(stable_ids))
                if len(members) < 2:
                    continue
                if any(not m["path"] or m.get("duration_ms") is None for m in members):
                    continue
                winner = _pick_canonical_tag(members)
                cur = dedup_conn.execute(
                    "INSERT INTO duplicate_clusters "
                    "(canonical_stable_id, canonical_path, rationale, flagged_manual_review, basis) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (winner["stable_id"], winner["path"], TAG_RATIONALE, 1, TAG_BASIS),
                )
                cluster_id = int(cur.lastrowid or 0)
                for row in members:
                    if row["stable_id"] == winner["stable_id"]:
                        continue
                    dedup_conn.execute(
                        "INSERT OR REPLACE INTO track_aliases "
                        "(alias_stable_id, alias_path, cluster_id, canonical_stable_id, similarity) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (row["stable_id"], row["path"], cluster_id, winner["stable_id"], TAG_SIMILARITY),
                    )
                    cluster_writer.writerow(
                        [
                            cluster_id,
                            winner["path"],
                            winner["stable_id"],
                            row["path"],
                            row["stable_id"],
                            f"{TAG_SIMILARITY:.4f}",
                            TAG_RATIONALE,
                        ]
                    )
                    manual_writer.writerow(
                        [cluster_id, MANUAL_REASON, winner["path"], row["path"], f"{TAG_SIMILARITY:.4f}", ""]
                    )
                written += 1

        dedup_conn.commit()
        return TagClusterStats(written=written, precision=None)
    finally:
        state_conn.close()
        dedup_conn.close()
