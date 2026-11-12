"""Tag-based duplicate clusters for the dedup review page."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.dedup import schema as dedup_schema
from apps.dedup.tag_clusters import TAG_BASIS, TAG_RATIONALE, run_tag_clusters
from apps.mik import duplicates as dups
from apps.mik.duplicates import artist_title_duration_groups
from apps.shared.state import db as state_db

A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _add_track(
    conn: sqlite3.Connection, stable_id: str, *, title: str, artist: str, path: str, duration_ms: int | None
) -> None:
    now = "2026-01-01T00:00:00Z"
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, file_path, duration_ms, created_at, updated_at) "
        "VALUES (?, 'inferred', ?, ?, ?, ?, ?, ?)",
        (stable_id, title, f'["{artist}"]', path, duration_ms, now, now),
    )


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = state_db.open_rw(db, apply_schema=True)
    _add_track(conn, A, title="Tamborel", artist="Quilla Ventrosk", path="/music/a.mp3", duration_ms=200_000)
    _add_track(conn, B, title="Tamborel", artist="Quilla Ventrosk", path="/music/b.mp3", duration_ms=200_500)
    conn.commit()
    conn.close()
    return db


@pytest.mark.requirement("SETPREP-10643")
def test_tag_grouping_matches_mik_duplicates_fixture(state_path: Path) -> None:
    """[if] tag groups are built [then] they match duplicates.py grouping, [else stop]."""
    conn = sqlite3.connect(state_path)
    try:
        exported = [sorted(g) for g in artist_title_duration_groups(conn)]
        via_groups = dups.duplicate_groups(conn, by_metadata=True)
        assert via_groups.members_of(A) == (A, B)
        assert exported == [[A, B]]
    finally:
        conn.close()


@pytest.mark.requirement("SETPREP-10643")
def test_tag_cluster_writes_unproven_rows(state_path: Path, tmp_path: Path) -> None:
    """[if] two tag duplicates exist [then] one unproven cluster is stored, [else stop]."""
    dedup_db = tmp_path / "dedup.sqlite"
    stats = run_tag_clusters(state_db=state_path, db_path=dedup_db)
    assert stats.written == 1
    conn = dedup_schema.ensure_schema(dedup_db)
    row = conn.execute(
        "SELECT duplicate_clusters.rationale, duplicate_clusters.basis, "
        "duplicate_clusters.flagged_manual_review, track_aliases.similarity "
        "FROM duplicate_clusters JOIN track_aliases USING (cluster_id) LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == TAG_RATIONALE
    assert row[1] == TAG_BASIS
    assert row[2] == 1
    assert row[3] == 0.0


@pytest.mark.requirement("SETPREP-10643")
def test_fingerprint_member_is_not_retagged(state_path: Path, tmp_path: Path) -> None:
    """[if] a row is already fingerprint-clustered [then] tags do not re-cluster it, [else stop]."""
    dedup_db = tmp_path / "dedup.sqlite"
    conn = dedup_schema.ensure_schema(dedup_db)
    conn.execute(
        "INSERT INTO duplicate_clusters (canonical_stable_id, canonical_path, rationale, basis) "
        "VALUES (?, ?, 'fingerprint', 'fingerprint')",
        (A, "/music/a.mp3"),
    )
    cid = conn.execute("SELECT cluster_id FROM duplicate_clusters").fetchone()[0]
    conn.execute(
        "INSERT INTO track_aliases (alias_stable_id, alias_path, cluster_id, canonical_stable_id, similarity) "
        "VALUES (?, ?, ?, ?, 0.99)",
        (B, "/music/b.mp3", cid, A),
    )
    conn.commit()
    conn.close()
    stats = run_tag_clusters(state_db=state_path, db_path=dedup_db)
    assert stats.written == 0
