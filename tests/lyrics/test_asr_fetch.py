"""ASR hub fetch failures fail loud without cache writes (LYRICS-07).

[if] ASR hub fetch fails or is unreachable [then] the service raises without writing cache or verdict rows, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.cloud import stem_index
from apps.cloud.lyrics_asr_source import LYRICS_ASR_HUB_UNREACHABLE
from apps.lyrics.asr_source import AsrLyricsProvider, LyricsAsrFetchError
from apps.lyrics.cache import cache_path
from apps.lyrics.fetch_verdicts import load_verdict
from apps.lyrics.service import LyricsFetchService, Track
from apps.sync_hub.transport import SyncTransportError

pytestmark = pytest.mark.requirement("LYRICS-07")


class UnreachableTransport:
    def post(self, path: str, payload: object) -> dict[str, object]:
        raise SyncTransportError("hub down")

    def get(self, path: str, params: object) -> dict[str, object]:
        raise SyncTransportError("hub down")


def _seed_track(db_path: Path, stable_id: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tracks ("
        "stable_id TEXT PRIMARY KEY, title TEXT, artists_json TEXT, "
        "duration_ms INTEGER, deleted_at TEXT)"
    )
    conn.execute(
        "INSERT INTO tracks (stable_id, title, artists_json, duration_ms, deleted_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (stable_id, "Title", '["Artist"]', 180_000),
    )
    conn.commit()
    conn.close()


def test_hub_unreachable_fails_without_cache_or_ledger(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "state.db"
    _seed_track(db_path, "hub-down")
    provider = AsrLyricsProvider(
        tmp_path,
        transport=UnreachableTransport(),
        machine_id="machine-a",
    )
    with pytest.raises(LyricsAsrFetchError) as excinfo:
        provider.fetch_transcript("hub-down")
    assert excinfo.value.code == LYRICS_ASR_HUB_UNREACHABLE
    assert not cache_path(tmp_path, "hub-down").exists()
    assert load_verdict(tmp_path, "hub-down") is None


def test_fetch_service_propagates_hub_error_without_cache(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "state.db"
    _seed_track(db_path, "hub-down")
    stem_index.save_cached_index(
        tmp_path,
        {
            "hub-down": {
                "manifest.json": "d" * 64,
                "vocals.wav": "c" * 64,
            }
        },
    )
    service = LyricsFetchService(
        tmp_path,
        provider=_AlwaysMissLrclib(),
        asr_provider=AsrLyricsProvider(
            tmp_path,
            transport=UnreachableTransport(),
            machine_id="machine-a",
        ),
    )
    with pytest.raises(LyricsAsrFetchError):
        service.fetch_or_resolve(
            Track("hub-down", "Artist", "Title", 180),
        )
    assert not cache_path(tmp_path, "hub-down").exists()
    assert load_verdict(tmp_path, "hub-down") is None


class _AlwaysMissLrclib:
    def fetch_synced(self, track: Track) -> str | None:
        return None
