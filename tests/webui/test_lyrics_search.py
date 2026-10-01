"""GET /api/v1/lyrics/search route tests (Part 3 of #935, issue #1344).

Builds a real state.db (StateWriter, same as test_search.py), a real lyrics
cache on disk (``apps.lyrics.cache.write``) and a real on-disk FTS5 index
(``apps.lyrics.search_index.index_batch``) under one data dir, so the route
test exercises the full path: index lookup -> track hydration -> snippet.
Nothing is stubbed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.lyrics.search_index import index_batch
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_db_path = state_dir / "state.db"
    conn = state_db.open_rw(state_db_path)
    bus = FakeEventBus()
    writer = StateWriter(conn, bus=bus, actor="test")
    try:
        writer.upsert_track(
            stable_id="s1",
            stable_id_tier="inferred",
            title="Lantern Orchard",
            artists=["Mira Valen"],
            album=None,
            isrc=None,
            duration_ms=203_000,
            file_path="/music/s1.mp3",
        )
        writer.upsert_track(
            stable_id="s2",
            stable_id_tier="inferred",
            title="Velvet Static",
            artists=["The Lowlands"],
            album=None,
            isrc=None,
            duration_ms=200_000,
            file_path="/music/s2.mp3",
        )
    finally:
        writer.close()
        conn.close()

    write(
        cache_path(tmp_path, "s1"),
        Lyrics(
            stable_id="s1",
            source="lrclib",
            lines=(
                LyricLine(start_ms=0, text="floating in the moonlight tonight"),
                LyricLine(start_ms=1000, text="lantern out of sight"),
            ),
        ),
    )
    write(
        cache_path(tmp_path, "s2"),
        Lyrics(
            stable_id="s2",
            source="lrclib",
            lines=(LyricLine(start_ms=0, text="blinded by the lights"),),
        ),
    )
    index_batch(tmp_path)
    return tmp_path


@pytest.fixture
def lyrics_client(data_dir: Path) -> Iterator[TestClient]:
    state_db_path = data_dir / "state" / "state.db"
    backend = SqliteBackend(state_db_path)
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c


def test_empty_query_returns_empty_results(lyrics_client: TestClient) -> None:
    r = lyrics_client.get("/api/v1/lyrics/search", params={"q": ""})
    assert r.status_code == 200
    assert r.json() == {"query": "", "items": [], "total": 0, "next_offset": None}


def test_production_app_registers_lyrics_search_contract() -> None:
    paths = create_app(mount_frontend=False).openapi()["paths"]
    assert "/api/v1/lyrics/search" in paths


def test_hit_carries_hydrated_row_plus_snippet(lyrics_client: TestClient) -> None:
    r = lyrics_client.get("/api/v1/lyrics/search", params={"q": "moonlight"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    hit = body["items"][0]
    assert hit["stable_id"] == "s1"
    assert hit["title"] == "Lantern Orchard"
    assert hit["artist"] == "Mira Valen"
    assert hit["match_context"] == "floating in the moonlight tonight"


def test_snippet_never_contains_the_full_transcript_or_a_bare_title(
    lyrics_client: TestClient,
) -> None:
    r = lyrics_client.get("/api/v1/lyrics/search", params={"q": "lights"})
    hit = r.json()["items"][0]
    assert hit["match_context"] == "blinded by the lights"
    assert hit["match_context"] != hit["title"]


def test_metadata_only_terms_are_not_lyric_matches(lyrics_client: TestClient) -> None:
    # "Mira Valen" is the artist, not in either cached transcript.
    r = lyrics_client.get("/api/v1/lyrics/search", params={"q": "mira valen"})
    assert r.json() == {"query": "mira valen", "items": [], "total": 0, "next_offset": None}


def test_query_with_no_lyric_index_yet_returns_honest_empty(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_db_path = state_dir / "state.db"
    conn = state_db.open_rw(state_db_path)
    conn.close()
    backend = SqliteBackend(state_db_path)
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    with TestClient(app) as c:
        r = c.get("/api/v1/lyrics/search", params={"q": "anything"})
    assert r.status_code == 200
    assert r.json() == {"query": "anything", "items": [], "total": 0, "next_offset": None}


def test_limit_bounds_are_enforced(lyrics_client: TestClient) -> None:
    r = lyrics_client.get("/api/v1/lyrics/search", params={"q": "moonlight", "limit": 0})
    assert r.status_code == 422
    r = lyrics_client.get("/api/v1/lyrics/search", params={"q": "moonlight", "limit": 10_000})
    assert r.status_code == 422


pytestmark = pytest.mark.rb_parity
