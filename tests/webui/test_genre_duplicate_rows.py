"""Duplicates share one genre in the Library rows, over HTTP and in the genre lane (GENRE-08).

- [if] one copy of a track was inferred [then] every copy's Library row shows that genre and its source, [else stop].
- [if] two copies are queued in the genre lane [then] both jobs settle done from ONE ask, [else stop].
- [if] every untagged track is queued [then] a duplicate group contributes one job, [else stop].
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.analysis import queue_user, queue_user_genre
from apps.analysis.store import open_conn
from apps.genre_infer import providers
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.genre_infer.pipeline_support import ASKED_ID, SAME_ARTIST_ID, ProviderServer, library, provider_server

pytestmark = pytest.mark.requirement("GENRE-08")

ASKED, OTHER = ASKED_ID, SAME_ARTIST_ID
COPIES = (ASKED, "t-twin", "t-twin-nofile")


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> Iterator[ProviderServer]:
    """The engine's provider settings (its four environment variables) pointed at the captured-reply server."""
    with provider_server() as srv:
        monkeypatch.setenv(providers.OPENROUTER_KEY_ENV, "k1")
        monkeypatch.setenv(providers.SERPER_KEY_ENV, "k2")
        monkeypatch.setenv(providers.OPENROUTER_ROOT_ENV, srv.base)
        monkeypatch.setenv(providers.SERPER_URL_ENV, srv.base + "/search")
        yield srv


def _library(tmp_path: Path) -> tuple[Path, Path]:
    """The shared four-track library plus two copies of its asked track, one of them with no file."""
    data, state = library(tmp_path)
    conn = sqlite3.connect(str(state))
    title, artists_json = conn.execute(
        "SELECT title, artists_json FROM tracks WHERE stable_id = ?", (ASKED,)
    ).fetchone()
    shouted = json.dumps([name.upper() for name in json.loads(artists_json)])
    conn.executemany(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, file_path, created_at, updated_at) "
        "VALUES (?, 'inferred', ?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        [
            ("t-twin", title.upper().replace(" ", "  ", 1), shouted, "/m/backup/twin.mp3"),
            ("t-twin-nofile", title, artists_json, None),
        ],
    )
    conn.commit()
    conn.close()
    return data, state


def _client(state: Path) -> TestClient:
    app = create_app(
        backend=SqliteBackend(state),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state),
        mount_frontend=False,
    )
    return TestClient(app)


def _rows(client: TestClient) -> dict[str, dict]:
    resp = client.get("/api/v1/tracks")
    assert resp.status_code == 200, resp.text
    return {r["stable_id"]: r for r in resp.json()["items"]}


def test_every_copys_row_shows_the_genre_one_copy_was_given(tmp_path: Path, live: ProviderServer) -> None:
    _data, state = _library(tmp_path)
    with _client(state) as client:
        before = _rows(client)
        assert [before[sid]["genre"] for sid in COPIES] == [None, None, None]
        out = client.post("/api/v1/genres/infer", json={"stable_ids": [ASKED]})
        assert out.status_code == 200, out.text
        rows = _rows(client)
        genre = rows[ASKED]["genre"]
        assert (
            genre and [(rows[sid]["genre"], rows[sid]["genre_source"]) for sid in COPIES] == [(genre, "odj-fused")] * 3
        )
        # Control: the artist's other track is not a copy and stays on its own tag.
        assert (rows[OTHER]["genre"], rows[OTHER]["genre_source"]) == ("Electronic", "library-tag")
        docs = client.get("/api/v1/genres", params={"ids": list(COPIES)}).json()["tracks"]
        assert {d["chosen"]["genre"] for d in docs.values()} == {genre}
        assert docs["t-twin-nofile"]["duplicates"] == [ASKED, "t-twin"]
        # An agent's finding on the copy with no file is every copy's row genre.
        attach = client.post(
            "/api/v1/genres/findings",
            json={
                "stable_id": "t-twin-nofile",
                "provider": "beatport",
                "genre": "Afro House",
                "confidence": 1.0,
                "source": "s",
            },
        )
        assert attach.status_code == 201, attach.text
        rows = _rows(client)
        assert [(rows[sid]["genre"], rows[sid]["genre_source"]) for sid in COPIES] == [("Afro House", "beatport")] * 3
        assert client.delete(f"/api/v1/genres/findings/{ASKED}/beatport").json() == {"removed": True}
        assert [_rows(client)[sid]["genre_source"] for sid in COPIES] == ["odj-fused"] * 3
        assert sum(live.hits.values()) == 3, "one search, one JEV call, one DeepSeek call for the whole group"


def _items(state: Path) -> dict[str, tuple[str, str | None]]:
    conn = sqlite3.connect(str(state))
    try:
        return {
            r[0]: (r[1], r[2])
            for r in conn.execute("SELECT stable_id, state, reason FROM analysis_queue_item WHERE lane = 'genre'")
        }
    finally:
        conn.close()


def test_the_genre_lane_settles_both_copies_from_one_ask(tmp_path: Path, live: ProviderServer) -> None:
    data, state = _library(tmp_path)
    conn = open_conn(state)
    try:
        queue_user.enqueue_next(
            conn, lane="genre", stable_ids=[ASKED, "t-twin"], stems_root=data / "stems", data_dir=data
        )
        assert (
            queue_user_genre.tick_genre(conn, runner_id="t", data_dir=data, stems_root=data / "stems", dry=False)
            == "ran"
        )
    finally:
        conn.close()
    assert _items(state) == {ASKED: ("done", None), "t-twin": ("done", None)}
    assert dict(live.hits) == {"/search": 1, "/alpha/decisions": 1, "/v1/chat/completions": 1}


def test_enqueue_all_missing_queues_one_job_per_group(tmp_path: Path, live: ProviderServer) -> None:
    """[if] every untagged track is queued [then] two present copies of one track are one job, [else stop]."""
    music = tmp_path / "music"
    music.mkdir()
    _data, state = _library(tmp_path)
    conn = sqlite3.connect(str(state))
    for sid in (ASKED, "t-twin", OTHER):
        path = music / f"{sid}.mp3"
        path.write_bytes(b"audio")
        conn.execute("UPDATE tracks SET file_path = ? WHERE stable_id = ?", (str(path), sid))
    conn.commit()
    conn.close()
    with _client(state) as client:
        out = client.post("/api/v1/genres/enqueue", json={"all_missing": True})
        assert out.status_code == 201, out.text
        assert out.json() == {"lane": "genre", "queued": 2, "requested": 0, "eligible": 2}
    assert sorted(_items(state)) == [ASKED, OTHER]
