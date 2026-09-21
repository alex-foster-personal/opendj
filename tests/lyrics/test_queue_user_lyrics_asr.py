"""Library-jobs lyrics lane ASR terminal outcomes (LYRICS-07).

[if] the library-jobs ASR lane finishes [then] terminal cache and verdict states match success or explicit failure, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.analysis import queue_store, queue_user, queue_user_runner
from apps.analysis.store import open_conn
from apps.cloud import stem_index
from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.lyrics.fetch_verdicts import FetchVerdict, utc_now_iso, write_verdict
from apps.lyrics.service import FetchResult

pytestmark = pytest.mark.requirement("LYRICS-07")


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = open_conn(db_path)
    queue_user.ensure_user_schema(conn)
    return conn


def _seed(db_path: Path, stable_ids: list[str]) -> None:
    conn = _conn(db_path)
    for stable_id in stable_ids:
        audio = db_path.parent / f"{stable_id}.wav"
        audio.write_bytes(b"\0")
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, duration_ms, "
            "file_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stable_id,
                "inferred",
                "Title",
                '["Artist"]',
                180_000,
                str(audio),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
    conn.commit()
    conn.close()


def _enqueue(
    conn: sqlite3.Connection, lane: str, stable_ids: list[str], data_dir: Path
) -> queue_user.EnqueueUserResult:
    return queue_user.enqueue_next(
        conn,
        lane=lane,
        stable_ids=stable_ids,
        placement="next",
        stems_root=data_dir / "stems",
        data_dir=data_dir,
    )


def test_instrumental_is_done_and_fresh(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    stable_id = "instrumental-track"
    _seed(db, [stable_id])
    vocals = "c" * 64
    stem_index.save_cached_index(
        tmp_path,
        {
            stable_id: {
                "manifest.json": "d" * 64,
                "vocals.wav": vocals,
            }
        },
    )
    conn = _conn(db)
    _enqueue(conn, "lyrics", [stable_id], tmp_path)

    def _run(sid: str) -> FetchResult:
        write_verdict(
            tmp_path,
            FetchVerdict(
                stable_id=sid,
                outcome="instrumental",
                source=None,
                vocals_sha256=vocals,
                hub_code=None,
                hub_message=None,
                language_iso3=None,
                recorded_at=utc_now_iso(),
            ),
        )
        return FetchResult(outcome="instrumental")

    outcome = queue_user_runner.tick_lane(
        conn,
        "lyrics",
        runner_id="runner-a",
        stems_root=tmp_path / "stems",
        data_dir=tmp_path,
        execute_lyrics=_run,
    )
    assert outcome == "ran"
    settled = queue_user.list_lane(conn, "lyrics", include_settled=True)
    assert settled[0].state == queue_store.ITEM_DONE
    assert settled[0].reason == "instrumental"
    assert queue_user.artifact_is_fresh(
        "lyrics", stable_id, stems_root=tmp_path / "stems", data_dir=tmp_path
    )
    conn.close()


def test_no_source_is_done_until_vocals_hash_changes(tmp_path: Path) -> None:
    stable_id = "no-source-track"
    vocals = "e" * 64
    write_verdict(
        tmp_path,
        FetchVerdict(
            stable_id=stable_id,
            outcome="no_source",
            source=None,
            vocals_sha256=vocals,
            hub_code="LYRICS_ASR_NOT_FOUND",
            hub_message="missing transcript",
            language_iso3=None,
            recorded_at=utc_now_iso(),
        ),
    )
    stem_index.save_cached_index(
        tmp_path,
        {
            stable_id: {
                "manifest.json": "f" * 64,
                "vocals.wav": vocals,
            }
        },
    )
    assert queue_user.artifact_is_fresh(
        "lyrics", stable_id, stems_root=tmp_path / "stems", data_dir=tmp_path
    )
    stem_index.save_cached_index(
        tmp_path,
        {
            stable_id: {
                "manifest.json": "f" * 64,
                "vocals.wav": "1" * 64,
            }
        },
    )
    assert not queue_user.artifact_is_fresh(
        "lyrics", stable_id, stems_root=tmp_path / "stems", data_dir=tmp_path
    )


def test_asr_cache_enqueue_is_skipped_when_fresh(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    stable_id = "asr-cached"
    _seed(db, [stable_id])
    write(
        cache_path(tmp_path, stable_id),
        Lyrics(stable_id, "asr", (LyricLine(1000, "Line"),)),
    )
    conn = _conn(db)
    result = _enqueue(conn, "lyrics", [stable_id], tmp_path)
    assert result.items[0].state == queue_store.ITEM_SKIPPED
    conn.close()
