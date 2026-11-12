"""A library lyrics job does not call a never-run track up to date (LYRICS-14).

[if] lyrics have no vocals hash [then] the lane runs and records no_lyrics_found, [else stop].
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.analysis import queue_store, queue_user, queue_user_runner
from apps.analysis.store import open_conn
from apps.cloud import stem_index
from apps.lyrics.cache import cache_path
from apps.lyrics.fetch_verdicts import FetchVerdict, load_verdict, utc_now_iso, write_verdict
from tests.lyrics.lyrics_asr_fixtures import FIXTURES

pytestmark = pytest.mark.requirement("LYRICS-14")

# Invented ASR cache: hallucination lines only.
_THANKS_ONLY = FIXTURES / "hallucination_thanks_only_cache.json"


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = open_conn(db_path)
    queue_user.ensure_user_schema(conn)
    return conn


def _seed(db_path: Path, stable_id: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _conn(db_path)
    audio = db_path.parent / f"{stable_id}.wav"
    audio.write_bytes(b"\0")
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, duration_ms, "
        "file_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            stable_id,
            "inferred",
            "Acceptance Voice",
            '["Open DJ Fixture"]',
            30_000,
            str(audio),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()


def _no_source(data_dir: Path, stable_id: str, vocals: str | None) -> None:
    write_verdict(
        data_dir,
        FetchVerdict(
            stable_id=stable_id,
            outcome="no_source",
            source=None,
            vocals_sha256=vocals,
            hub_code="LYRICS_ASR_NOT_FOUND",
            hub_message="track is not in the local stem bundle index",
            language_iso3=None,
            recorded_at=utc_now_iso(),
        ),
    )


def _thanks_only_id() -> str:
    payload = json.loads(_THANKS_ONLY.read_text(encoding="utf-8"))
    stable_id = payload["stable_id"]
    if not isinstance(stable_id, str) or not stable_id:
        raise AssertionError("thanks-only fixture has no stable_id")
    return stable_id


def test_a_null_vocals_hash_miss_runs_and_records_no_lyrics_found(tmp_path: Path) -> None:
    stable_id = _thanks_only_id()
    db = tmp_path / "state" / "state.db"
    _seed(db, stable_id)
    _no_source(tmp_path, stable_id, None)
    # The real fetch reads this cache and resolves instrumental. No callback.
    destination = cache_path(tmp_path, stable_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_THANKS_ONLY, destination)
    original = _THANKS_ONLY.read_bytes()
    conn = _conn(db)
    queued = queue_user.enqueue_next(
        conn,
        lane="lyrics",
        stable_ids=[stable_id],
        stems_root=tmp_path / "stems",
        data_dir=tmp_path,
    )
    assert queued.items[0].state == queue_store.ITEM_PENDING
    assert queued.items[0].reason is None

    assert (
        queue_user_runner.tick_lane(
            conn,
            "lyrics",
            runner_id="lyrics-runner",
            stems_root=tmp_path / "stems",
            data_dir=tmp_path,
        )
        == "ran"
    )
    settled = queue_user.list_lane(conn, "lyrics", include_settled=True)
    assert settled[0].state == queue_store.ITEM_DONE
    assert settled[0].reason == "no_lyrics_found"
    assert settled[0].started_at
    verdict = load_verdict(tmp_path, stable_id)
    assert verdict is not None and verdict.outcome == "instrumental"
    assert destination.read_bytes() == original
    again = queue_user.enqueue_next(
        conn,
        lane="lyrics",
        stable_ids=[stable_id],
        stems_root=tmp_path / "stems",
        data_dir=tmp_path,
    )
    assert again.items[0].state == queue_store.ITEM_SKIPPED
    assert again.items[0].reason == "no_lyrics_found"
    conn.close()


def test_a_miss_that_names_the_current_vocals_hash_is_not_up_to_date(tmp_path: Path) -> None:
    stable_id = "voice-looked-up"
    vocals = "a" * 64
    db = tmp_path / "state.db"
    _seed(db, stable_id)
    stem_index.save_cached_index(
        tmp_path, {stable_id: {"manifest.json": "b" * 64, "vocals.wav": vocals}}
    )
    _no_source(tmp_path, stable_id, vocals)
    conn = _conn(db)
    queued = queue_user.enqueue_next(
        conn,
        lane="lyrics",
        stable_ids=[stable_id],
        stems_root=tmp_path / "stems",
        data_dir=tmp_path,
    )
    assert queued.items[0].state == queue_store.ITEM_SKIPPED
    assert queued.items[0].reason == "no_lyrics_found"
    assert queued.items[0].started_at is None
    conn.close()

def test_a_non_asr_cache_without_lines_is_not_fresh() -> None:
    from apps.analysis.queue_user import _cache_has_lyric_line
    from apps.lyrics.cache import LyricLine, Lyrics

    assert _cache_has_lyric_line(Lyrics("sid", "lrclib", ())) is False
    assert _cache_has_lyric_line(Lyrics("sid", "lrclib", (LyricLine(1000, "Line"),))) is True

