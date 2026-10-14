"""LRCLIB-first then ASR orchestration for line lyrics (LYRICS-07).

[if] LRCLIB returns synced lyrics [then] ASR is not fetched and cache reflects LRCLIB, [else stop].
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.cloud import stem_index
from apps.lyrics.asr_hallucination import ASR_SOURCE
from apps.lyrics.asr_source import AsrLyricsProvider
from apps.lyrics.cache import LyricLine, Lyrics, cache_path, load, write
from apps.lyrics.fetch_verdicts import load_verdict, verdict_path
from apps.lyrics.lrclib_query import asr_recheck_due
from apps.lyrics.service import LrclibProvider, LyricsFetchService, Track

pytestmark = pytest.mark.requirement("LYRICS-07")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lyrics_asr" / "sample.json"


class RecordingLrclib:
    def __init__(self, synced: str | None) -> None:
        self.synced = synced
        self.calls = 0

    def fetch_synced(self, track: Track) -> str | None:
        self.calls += 1
        return self.synced


LRCLIB_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lrclib_get"


def lrclib_capture(name: str) -> tuple[int, bytes]:
    """A captured LRCLIB /api/get response, checked against its manifest first."""
    manifest = json.loads((LRCLIB_FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
    entry = manifest["files"][name]
    body = (LRCLIB_FIXTURES / name).read_bytes()
    assert hashlib.sha256(body).hexdigest() == entry["sha256"], f"{name} drifted from MANIFEST.json"
    return int(entry["status"]), body


@contextmanager
def lrclib_endpoint(
    answers: list[tuple[int, bytes]],
) -> Iterator[tuple[LrclibProvider, list[dict[str, list[str]]]]]:
    """Serve captured LRCLIB responses on loopback HTTP to the production LrclibProvider.

    Each request takes the next answer in order, byte for byte, and records its
    query string, so a test sees the real URL encoding, status handling and JSON
    decoding.
    """
    queries: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            queries.append(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
            status, data = answers[len(queries) - 1]
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield LrclibProvider(f"http://127.0.0.1:{port}/api/get"), queries
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


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


def test_verdict_path_refuses_a_traversing_stable_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes lyrics-fetch"):
        verdict_path(tmp_path, "../../etc/passwd")


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


def test_lrclib_query_drops_mik_prefix(tmp_path: Path) -> None:
    """A Mixed In Key energy prefix never reaches LRCLIB; the version suffix does.

    Shape of the reported case (invented title): "7 - Paper Lanterns - Radio
    Edit" 404s on LRCLIB, the bare "Paper Lanterns - Radio Edit" hits, and the
    miss used to serve Whisper's misheard chorus.
    """
    with lrclib_endpoint([lrclib_capture("hit.json")]) as (provider, queries):
        track = Track("paper-lanterns", "Tamsin Quell", "7 - Paper Lanterns - Radio Edit", 202)
        result = LyricsFetchService(tmp_path, provider=provider).fetch_or_resolve(track)
    assert queries == [
        {
            "artist_name": ["Tamsin Quell"],
            "track_name": ["Paper Lanterns - Radio Edit"],
            "duration": ["202"],
        }
    ]
    assert result.lyrics is not None and result.lyrics.source == "lrclib"


def _seed_asr_cache(tmp_path: Path, stable_id: str) -> None:
    write(
        cache_path(tmp_path, stable_id),
        Lyrics(
            stable_id,
            ASR_SOURCE,
            (
                LyricLine(1000, "Hold the paper, paper, lantern, lantern, light"),
                LyricLine(4000, "Carry it down to the water"),
            ),
        ),
    )


def test_asr_cache_is_replaced_when_lrclib_now_hits(tmp_path: Path) -> None:
    stable_id = "asr-then-lrclib"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _seed_track(state_dir / "state.db", stable_id)
    _seed_asr_cache(tmp_path, stable_id)
    with lrclib_endpoint([lrclib_capture("hit.json")]) as (provider, queries):
        service = LyricsFetchService(tmp_path, provider=provider)
        result = service.fetch_or_resolve(Track(stable_id, "Artist", "Title", 180))
    assert len(queries) == 1
    assert result.outcome == "cached"
    assert result.lyrics is not None and result.lyrics.source == "lrclib"
    cached = load(cache_path(tmp_path, stable_id))
    assert cached is not None and cached.source == "lrclib"
    assert cached.lines[0] == LyricLine(19820, "Hold the paper lantern high")


def test_asr_cache_still_served_when_lrclib_misses_or_fails(tmp_path: Path) -> None:
    stable_id = "asr-stays"
    _seed_asr_cache(tmp_path, stable_id)
    answers = [lrclib_capture("not_found.json"), lrclib_capture("server_overloaded.json")]
    with lrclib_endpoint(answers) as (provider, queries):
        service = LyricsFetchService(tmp_path, provider=provider)
        for asked in range(1, len(answers) + 1):
            result = service.fetch_or_resolve(Track(stable_id, "Artist", "Title", 180))
            assert len(queries) == asked
            assert result.outcome == "cached"
            assert result.lyrics is not None and result.lyrics.source == ASR_SOURCE
    verdict = load_verdict(tmp_path, stable_id)
    assert verdict is not None and verdict.source == ASR_SOURCE
    assert not asr_recheck_due(verdict, datetime.now(UTC))


def test_lrclib_cache_is_not_refetched(tmp_path: Path) -> None:
    stable_id = "lrclib-cached"
    write(cache_path(tmp_path, stable_id), Lyrics(stable_id, "lrclib", (LyricLine(0, "Line"),)))
    with lrclib_endpoint([lrclib_capture("hit.json")]) as (provider, queries):
        service = LyricsFetchService(tmp_path, provider=provider)
        result = service.fetch_or_resolve(Track(stable_id, "Artist", "Title", 180))
    assert queries == []
    assert result.lyrics is not None and result.lyrics.lines[0].text == "Line"
