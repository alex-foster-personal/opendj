"""OJ's library tools carry the engine's ETag: read, then write under If-Match.

The webui app is built by ``create_app`` over a fresh sqlite state.db and
served by uvicorn on an ephemeral loopback port; ``library_read`` and
``library_write`` run their real ``run`` against it, as the agent loop does
once a call is approved. No mocks.

[if] OJ cannot send the read etag as If-Match [then] playlist rename and reorder always fail 428, [else stop].
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure
from apps.engine_core.assistant.core_tools import CORE_TOOLS, EngineAnswer, engine_call
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread

TOOLS = {spec.name: spec for spec in CORE_TOOLS}
PLAYLISTS = "/api/v1/playlists"


@pytest.fixture
def base_url(tmp_path: Path) -> Iterator[str]:
    db = tmp_path / "state.db"
    state_db.open_rw(db, apply_schema=True).close()
    app = create_app(
        backend=SqliteBackend(db),
        hostname="test-host",
        state_db_path=str(db),
        mount_frontend=False,
        client_error_log_dir=tmp_path / "client-errors",
        client_event_log_dir=tmp_path / "client-events",
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the core-tools etag webui")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15.0)


def _ctx(base: str) -> ToolContext:
    return ToolContext(base_url=base, audible=False)


def _run(base: str, name: str, args: dict[str, Any]) -> Any:
    return asyncio.run(TOOLS[name].run(_ctx(base), args))


def _create(base: str, name: str) -> dict[str, Any]:
    return _run(base, "library_write", {"method": "POST", "path": PLAYLISTS, "body": {"name": name}})


def _read_playlist(base: str, playlist_id: str) -> dict[str, Any]:
    return _run(base, "library_read", {"path": f"{PLAYLISTS}/{playlist_id}"})


# ----- read then write -----------------------------------------------------------
def test_a_rename_under_the_read_etag_lands(base_url: str) -> None:
    """[if] OJ renames a playlist with if_match set to the read etag [then] the engine answers 200 and the name changes, [else stop]."""
    created = _create(base_url, "Warmup")
    read = _read_playlist(base_url, created["playlist_id"])
    assert set(read) == {"etag", "body"}
    assert read["body"]["name"] == "Warmup"
    renamed = _run(
        base_url,
        "library_write",
        {
            "method": "PATCH",
            "path": f"{PLAYLISTS}/{created['playlist_id']}",
            "body": {"name": "Peak Time"},
            "if_match": read["etag"],
        },
    )
    assert renamed["name"] == "Peak Time"
    assert _read_playlist(base_url, created["playlist_id"])["body"]["name"] == "Peak Time"


def test_a_stale_etag_fails_engine_error_and_changes_nothing(base_url: str) -> None:
    """[if] if_match is stale [then] the tool fails engine_error naming the engine's status and the name stays, [else stop]."""
    created = _create(base_url, "Opener")
    stale = _read_playlist(base_url, created["playlist_id"])["etag"]
    path = f"{PLAYLISTS}/{created['playlist_id']}"
    _run(base_url, "library_write", {"method": "PATCH", "path": path, "body": {"name": "Second"}, "if_match": stale})
    with pytest.raises(ToolFailure) as caught:
        _run(base_url, "library_write", {"method": "PATCH", "path": path, "body": {"name": "Third"}, "if_match": stale})
    assert caught.value.code == "engine_error"
    assert f"PATCH {path} -> 409" in caught.value.message
    assert _read_playlist(base_url, created["playlist_id"])["body"]["name"] == "Second"


def test_a_get_with_no_etag_returns_the_bare_body(base_url: str) -> None:
    """[if] a GET answer carries no ETag [then] library_read returns the bare body exactly as today, [else stop]."""
    _create(base_url, "Listed")
    direct = httpx.get(f"{base_url}{PLAYLISTS}", timeout=10.0)
    assert "etag" not in direct.headers
    assert _run(base_url, "library_read", {"path": PLAYLISTS}) == direct.json()


def test_a_create_without_if_match_behaves_as_today(base_url: str) -> None:
    """[if] library_write is called without if_match on a create [then] it answers the bare created row, [else stop]."""
    created = _create(base_url, "Closer")
    assert created["name"] == "Closer"
    assert "etag" not in created
    listed = httpx.get(f"{base_url}{PLAYLISTS}", timeout=10.0).json()
    assert created["playlist_id"] in {row["playlist_id"] for row in listed}


def test_engine_call_hands_back_the_body_and_etag(base_url: str) -> None:
    """[if] engine_call drops the ETag header [then] the typed OJ tools can never chain a guarded write, [else stop]."""
    created = _create(base_url, "Helper")
    answer = asyncio.run(engine_call(_ctx(base_url), "GET", f"{PLAYLISTS}/{created['playlist_id']}"))
    assert isinstance(answer, EngineAnswer)
    assert answer.body["name"] == "Helper"
    assert answer.etag is not None and answer.etag.startswith('"')
