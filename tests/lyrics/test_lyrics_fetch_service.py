"""LRCLIB-first then ASR orchestration for line lyrics (LYRICS-07).

[if] LRCLIB returns synced lyrics [then] ASR is not fetched and cache reflects LRCLIB, [else stop].
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from apps.cloud import stem_index
from apps.lyrics.asr_source import AsrLyricsProvider
from apps.lyrics.cache import cache_path, load
from apps.lyrics.fetch_verdicts import load_verdict
from apps.lyrics.service import LyricsFetchService, Track

pytestmark = pytest.mark.requirement("LYRICS-07")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lyrics_asr" / "sample.json"


class RecordingLrclib:
    def __init__(self, synced: str | None) -> None:
        self.synced = synced
        self.calls = 0

    def fetch_synced(self, track: Track) -> str | None:
        self.calls += 1
        return self.synced


class PresignTransport:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies

    def post(self, path: str, payload: object) -> dict[str, object]:
        raise AssertionError("unexpected POST")

    def get(self, path: str, params: object) -> dict[str, object]:
        stable_id = path.rsplit("/", 1)[-1]
        body = self.bodies[stable_id]
        digest = hashlib.sha256(body).hexdigest()
        return {
            "stable_id": stable_id,
            "expires_in_seconds": 900,
            "url": f"http://127.0.0.1/asr/{stable_id}",
            "content_hash": digest,
            "size_bytes": len(body),
        }


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


def _seed_stem_index(tmp_path: Path, stable_id: str, vocals_digest: str) -> None:
    stem_index.save_cached_index(
        tmp_path,
        {
            stable_id: {
                "manifest.json": "b" * 64,
                "vocals.wav": vocals_digest,
            }
        },
    )


def test_lrclib_hit_skips_asr(tmp_path: Path) -> None:
    stable_id = "lrclib-only"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _seed_track(state_dir / "state.db", stable_id)
    lrclib = RecordingLrclib("[00:01.00]Line one")
    service = LyricsFetchService(tmp_path, provider=lrclib)
    result = service.fetch_or_resolve(Track(stable_id, "Artist", "Title", 180))
    assert result.outcome == "cached"
    assert result.lyrics is not None and result.lyrics.source == "lrclib"
    assert lrclib.calls == 1
    assert load(cache_path(tmp_path, stable_id)) is not None


def test_asr_hit_writes_cache_and_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stable_id = "asr-sample-track"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _seed_track(state_dir / "state.db", stable_id)
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["stable_id"] = stable_id
    body = (json.dumps(payload) + "\n").encode("utf-8")
    vocals_digest = payload["vocals_sha256"]
    _seed_stem_index(tmp_path, stable_id, vocals_digest)
    transport = PresignTransport({stable_id: body})

    def _fake_fetch(url: str, content_hash: str) -> bytes:
        assert hashlib.sha256(body).hexdigest() == content_hash
        return body

    monkeypatch.setattr("apps.cloud.asset_store.fetch_presigned_bytes", _fake_fetch)
    service = LyricsFetchService(
        tmp_path,
        provider=RecordingLrclib(None),
        asr_provider=AsrLyricsProvider(
            tmp_path,
            transport=transport,
            machine_id="machine-a",
        ),
    )
    result = service.fetch_or_resolve(Track(stable_id, "Artist", "Title", 180))
    assert result.outcome == "cached"
    cached = load(cache_path(tmp_path, stable_id))
    assert cached is not None and cached.source == "asr"
    verdict = load_verdict(tmp_path, stable_id)
    assert verdict is not None and verdict.outcome == "cached" and verdict.source == "asr"
