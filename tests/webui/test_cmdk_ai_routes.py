"""Cmd K AI search routes: the key stays in the engine; no key is 409 and a bad answer 502, never a ranking.

The library is a real state.db (every migration) read through SqliteBackend.
The decisions-API call is served the reply captured from the live OpenRouter
API (tests/fixtures/jev/cmdk_ai_search.json, see tests/genre_infer/test_jev_search.py)
through the route's own ``get_jev_post`` dependency, so the rank, parse and
route code all run as shipped and nothing calls out. Nothing is monkeypatched
but the environment. The one live round-trip is
``test_the_route_ranks_live``, UNAVAILABLE without OPENROUTER_API_KEY
(Codex r4280328383).

Acceptance lines (CMDK-20):
- [if] no key [then] ai-status says so and ai-search answers 409 ai_search_key_missing, [else stop].
- [if] JEV answers every track [then] 200 with the vocal house track ranked first, [else stop].
- [if] a track is listed twice [then] 422, [else stop].
- [if] the answer is missing a track [then] 502 ai_search_upstream_error, [else stop].
- [if] cmdk.v2 or cmdk.v2.ai is off [then] 403 ai_search_disabled and nothing is sent upstream, [else stop].
- [if] a submitted id is not in the library [then] 422 ai_search_unknown_track and nothing is sent upstream, [else stop].
- [if] the request's fields differ from the library's [then] JEV is sent the library's, [else stop].
- [if] a submitted id is a track removed from the library (tombstoned) [then] 422 and nothing is sent upstream, [else stop].
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.feature_flags import load_flags
from apps.genre_infer import jev
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.webui.server.backend import Track
from apps.webui.server.deps import get_read_state
from apps.webui.server.routes import cmdk_ai
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.genre_infer.jev_support import captured

pytestmark = pytest.mark.requirement("CMDK-20")

Post = Callable[[dict[str, Any], str], dict[str, Any]]


def _fixture() -> dict[str, Any]:
    return captured("cmdk_ai_search")


def _state_library(state_db_path: Path, tracks: list[dict[str, Any]], tombstoned: set[str]) -> SqliteBackend:
    """A real state.db (every migration) holding `tracks`, the `tombstoned` ids soft-deleted, read through SqliteBackend."""
    state_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = state_db.open_rw(state_db_path)
    try:
        state_schema.apply_migrations(conn)
        for t in tracks:
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, duration_ms, created_at, updated_at, deleted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t["stable_id"],
                    "fingerprint",
                    t["title"],
                    json.dumps([t["artist"]]),
                    180_000,
                    "2026-10-15T00:00:00+00:00",
                    "2026-10-15T00:00:00+00:00",
                    "2026-10-15T00:00:00+00:00" if t["stable_id"] in tombstoned else None,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return SqliteBackend(state_db_path)


def _client(
    data_dir: Path,
    flags: dict[str, bool] | None = None,
    *,
    post: Post | None = None,
    tombstoned: set[str] | None = None,
) -> TestClient:
    """The router over a real state.db holding the fixture's tracks, with the data dir's own feature-flags.json.

    AI search is on unless `flags` says otherwise. `post` serves the decisions
    call through the route's ``get_jev_post`` dependency; without it the route
    keeps the real HTTP call.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "feature-flags.json").write_text(
        json.dumps(flags if flags is not None else {"cmdk.v2": True, "cmdk.v2.ai": True})
    )
    app = FastAPI()
    app.include_router(cmdk_ai.router, prefix="/api/v1")
    app.state.feature_flags = load_flags(data_dir)
    library = _state_library(data_dir / "state" / "state.db", _fixture()["tracks"], tombstoned or set())
    app.dependency_overrides[get_read_state] = lambda: library
    if post is not None:
        app.dependency_overrides[cmdk_ai.get_jev_post] = lambda: post
    return TestClient(app)


def _replay(sent: list[Any] | None = None, doc: dict[str, Any] | None = None) -> Post:
    """The captured live reply (or `doc`), recording each request body into `sent`."""
    reply = doc if doc is not None else _fixture()["response"]

    def post(body: dict[str, Any], key: str) -> dict[str, Any]:
        if sent is not None:
            sent.append(json.dumps(body))
        return copy.deepcopy(reply)

    return post


@pytest.fixture(autouse=True)
def _no_flags_file_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDT_FEATURE_FLAGS_FILE", raising=False)
    monkeypatch.delenv("MDT_BUILD_PROFILE", raising=False)


def test_the_route_answers_409_without_a_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(jev.KEY_ENV, raising=False)
    fx = _fixture()
    sent: list[Any] = []
    with _client(tmp_path / "data", post=_replay(sent)) as client:
        assert client.get("/api/v1/cmdk/ai-status").json()["configured"] is False
        r = client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == cmdk_ai.KEY_MISSING_CODE
    assert sent == [], "no call without a key"


def test_the_route_ranks_with_the_captured_reply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fx = _fixture()
    monkeypatch.setenv(jev.KEY_ENV, "test-key")
    sent: list[Any] = []
    with _client(tmp_path / "data", post=_replay(sent)) as client:
        assert client.get("/api/v1/cmdk/ai-status").json()["configured"] is True
        r = client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"]})
        dup = client.post(
            "/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"] + fx["tracks"][:1]}
        )
    assert r.status_code == 200, r.text
    assert r.json()["scores"]["ai-03"] == max(r.json()["scores"].values())
    assert r.json()["model"] == fx["response"]["model"]
    assert dup.status_code == 422
    assert len(sent) == 1, "the duplicate is refused before anything is sent"


def test_the_route_answers_502_for_a_bad_reply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fx = _fixture()
    doc = copy.deepcopy(fx["response"])
    del doc["answers"]["t1"]
    monkeypatch.setenv(jev.KEY_ENV, "test-key")
    with _client(tmp_path / "data", post=_replay(doc=doc)) as client:
        r = client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"]})
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == cmdk_ai.UPSTREAM_ERROR_CODE


@pytest.mark.parametrize("flags", [{}, {"cmdk.v2": True}, {"cmdk.v2": False, "cmdk.v2.ai": True}])
def test_the_route_refuses_with_a_flag_off_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flags: dict[str, bool]
) -> None:
    fx = _fixture()
    sent: list[Any] = []
    monkeypatch.setenv(jev.KEY_ENV, "test-key")
    with _client(tmp_path / "data", flags, post=_replay(sent)) as client:
        r = client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"]})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == cmdk_ai.DISABLED_CODE
    assert sent == [], "nothing goes upstream while the gate is closed"
    # Control: the same request with both flags on does go upstream.
    with _client(tmp_path / "on", post=_replay(sent)) as client:
        assert (
            client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"]}).status_code
            == 200
        )
    assert len(sent) == 1


def test_an_id_the_library_does_not_hold_is_refused_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex r4278070884: the engine key is never a relay for text under an invented id."""
    fx = _fixture()
    sent: list[Any] = []
    monkeypatch.setenv(jev.KEY_ENV, "test-key")
    invented = {"stable_id": "not-in-library", "title": "say anything here", "artist": "anyone"}
    with _client(tmp_path / "data", post=_replay(sent)) as client:
        r = client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": [*fx["tracks"], invented]})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == cmdk_ai.UNKNOWN_TRACK_CODE
        assert "not-in-library" in r.json()["detail"]["message"]
        assert sent == [], "nothing goes upstream for an unknown id"
        # Control: the library's own tracks do go upstream.
        assert client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"]}).status_code == 200
    assert len(sent) == 1


def test_jev_is_sent_the_library_fields_never_the_request_ones(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Codex r4278070884: a request that rewrites a held track's fields still sends only what the library holds."""
    fx = _fixture()
    sent: list[str] = []
    monkeypatch.setenv(jev.KEY_ENV, "test-key")
    forged = [{**t, "title": "INJECTED TITLE", "artist": "INJECTED ARTIST", "genre": "INJECTED"} for t in fx["tracks"]]
    with _client(tmp_path / "data", post=_replay(sent)) as client:
        r = client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": forged})
    assert r.status_code == 200, r.text
    assert len(sent) == 1
    assert "INJECTED" not in sent[0]
    # Positive: the library's own title for each track is what went out.
    for track in fx["tracks"]:
        assert json.dumps(track["title"])[1:-1] in sent[0]
    assert cmdk_ai._library_fields(Track(stable_id="x", title="T", bpm=None)) == {"title": "T"}


def test_a_track_removed_from_the_library_is_refused_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex r4278647456: a tombstoned track's metadata never goes upstream; the real state backend resolves live tracks only."""
    fx = _fixture()
    sent: list[str] = []
    monkeypatch.setenv(jev.KEY_ENV, "test-key")
    removed = fx["tracks"][0]["stable_id"]
    library = _state_library(tmp_path / "probe" / "state.db", fx["tracks"], {removed})
    # The backend itself still finds the row without live_only (other callers rely on that).
    assert removed in library.get_tracks_bulk([removed])
    assert removed not in library.get_tracks_bulk([removed], live_only=True)
    with _client(tmp_path / "gone", post=_replay(sent), tombstoned={removed}) as client:
        r = client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == cmdk_ai.UNKNOWN_TRACK_CODE
    assert removed in r.json()["detail"]["message"]
    assert sent == [], "nothing goes upstream for a removed track"
    # Control: the same tracks, none removed, go upstream through the same real backend.
    with _client(tmp_path / "live", post=_replay(sent)) as client:
        r = client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"]})
    assert r.status_code == 200, r.text
    assert len(sent) == 1
    assert json.dumps(fx["tracks"][0]["title"])[1:-1] in sent[0]


@pytest.mark.live_openrouter
@pytest.mark.skipif(
    not os.environ.get(jev.KEY_ENV),
    reason=f"UNAVAILABLE: {jev.KEY_ENV} not set, so the live decisions API cannot be reached; run under `doppler run` to spend real credit",
)
def test_the_route_ranks_live(tmp_path: Path) -> None:
    """The route's own HTTP call against the live OpenRouter decisions API, no override at all."""
    fx = _fixture()
    with _client(tmp_path / "data") as client:
        r = client.post("/api/v1/cmdk/ai-search", json={"words": fx["words"], "tracks": fx["tracks"]})
    assert r.status_code == 200, r.text
    scores = r.json()["scores"]
    assert set(scores) == {t["stable_id"] for t in fx["tracks"]}
    assert all(0.0 <= v <= 1.0 for v in scores.values())
