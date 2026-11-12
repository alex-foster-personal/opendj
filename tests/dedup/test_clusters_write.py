"""Tests for apps.dedup.clusters_write.record_hash_duplicate.

[if] audio_hash pairs are recorded [then] clusters and aliases are idempotent, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.dedup import schema as dedup_schema
from apps.dedup.clusters_write import record_hash_duplicate
from apps.dedup.http import open_hash_duplicate_recorder

pytestmark = pytest.mark.requirement("SETPREP-10626")


def _open(tmp_path: Path):
    db = tmp_path / "phase7.sqlite"
    return db, dedup_schema.ensure_schema(db)


def test_same_audio_hash_pair_writes_one_cluster_and_alias(
    tmp_path: Path,
) -> None:
    """[if] a new id shares audio_hash with a live row [then] one audio_hash cluster is written, [else stop]."""
    _, conn = _open(tmp_path)
    try:
        assert record_hash_duplicate(
            conn,
            "new-id",
            "old-id",
            "sha256:" + "ab" * 32,
            new_path="/b/copy.mp3",
            existing_path="/a/orig.mp3",
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM duplicate_clusters").fetchone()[0] == 1
        row = conn.execute("SELECT rationale, canonical_stable_id FROM duplicate_clusters").fetchone()
        assert row[0] == "audio_hash"
        assert row[1] == "old-id"
        alias = conn.execute("SELECT alias_stable_id, similarity FROM track_aliases").fetchone()
        assert alias == ("new-id", 1.0)
    finally:
        conn.close()


def test_repeating_the_same_pair_writes_nothing_new(tmp_path: Path) -> None:
    """[if] the same pair is recorded again [then] row counts stay unchanged, [else stop]."""
    _, conn = _open(tmp_path)
    args = (
        conn,
        "new-id",
        "old-id",
        "sha256:" + "ab" * 32,
    )
    kwargs = {"new_path": "/b/copy.mp3", "existing_path": "/a/orig.mp3"}
    try:
        assert record_hash_duplicate(*args, **kwargs)
        conn.commit()
        assert record_hash_duplicate(*args, **kwargs) is False
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM duplicate_clusters").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM track_aliases").fetchone()[0] == 1
    finally:
        conn.close()


def test_recorder_from_the_http_surface_records_commits_and_closes(tmp_path: Path) -> None:
    """[if] ingest drives the injected recorder [then] its pair lands once and survives close, [else stop]."""
    db = tmp_path / "dedup" / "phase7.sqlite"
    recorder = open_hash_duplicate_recorder(db)
    try:
        kwargs = {"new_path": "/b/copy.mp3", "existing_path": "/a/orig.mp3"}
        assert recorder.record("new-id", "old-id", "sha256:" + "cd" * 32, **kwargs)
        assert recorder.record("new-id", "old-id", "sha256:" + "cd" * 32, **kwargs) is False
        recorder.commit()
    finally:
        recorder.close()
    conn = dedup_schema.ensure_schema(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM duplicate_clusters").fetchone()[0] == 1
        assert conn.execute("SELECT alias_stable_id, similarity FROM track_aliases").fetchone() == ("new-id", 1.0)
    finally:
        conn.close()
