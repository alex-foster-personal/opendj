"""The playlist fill page fits the tracks route's limit cap (LIBM-134).

The web client fills a playlist with ``PLAYLIST_FILL_PAGE``-row pages after its
30-row first page. The route must accept that page size, and must still refuse
anything larger, so one request's hydration stays bounded.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track

from .conftest import _stub_rb_vendor

_FILL_TS = (
    Path(__file__).resolve().parents[2]
    / "apps/webui/frontend/src/lib/components/rb/browser/fill-playlist-pane.ts"
)
_ROUTE_CAP = 500


def _client_fill_page() -> int:
    match = re.search(r"^export const PLAYLIST_FILL_PAGE = (\d+);$", _FILL_TS.read_text(), re.M)
    assert match, f"PLAYLIST_FILL_PAGE not found in {_FILL_TS}"
    return int(match.group(1))


def _client(monkeypatch, tmp_path: Path, members: int) -> TestClient:
    _stub_rb_vendor(monkeypatch)
    backend = InMemoryBackend()
    items: list[str] = []
    for i in range(members):
        sid = f"fill-cap-{i:04d}"
        path = tmp_path / f"{sid}.mp3"
        path.write_bytes(b"\x00")
        backend.seed_track(Track(stable_id=sid, file_path=str(path), title=f"Track {i}"))
        items.append(sid)
    backend.seed_playlist(Playlist(playlist_id="pl-fill", name="Fill", vendor="djay", items=items))
    app = create_app(
        backend=backend, bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, syncthing_status_fn=lambda: None,
    )
    return TestClient(app)


@pytest.mark.requirement("LIBM-134")
def test_route_serves_a_full_client_fill_page(monkeypatch, tmp_path: Path):
    """[if] the client asks for one fill page [then] the route answers 200 with that many rows, [else stop]."""
    fill_page = _client_fill_page()
    assert fill_page <= _ROUTE_CAP, f"client fill page {fill_page} exceeds the route cap {_ROUTE_CAP}"
    with _client(monkeypatch, tmp_path, fill_page + 40) as c:
        r = c.get("/api/v1/playlists/pl-fill/tracks", params={"limit": fill_page, "offset": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["tracks"]) == fill_page
    assert body["tracks"][0]["stable_id"] == "fill-cap-0030"
    assert body["next_offset"] == 30 + fill_page


@pytest.mark.requirement("LIBM-134")
def test_route_refuses_a_page_over_its_cap(monkeypatch, tmp_path: Path):
    """[if] a page over the cap is asked for [then] the route answers 422, never an unbounded read, [else stop]."""
    with _client(monkeypatch, tmp_path, 3) as c:
        ok = c.get("/api/v1/playlists/pl-fill/tracks", params={"limit": _ROUTE_CAP, "offset": 0})
        over = c.get("/api/v1/playlists/pl-fill/tracks", params={"limit": _ROUTE_CAP + 1, "offset": 0})
    assert ok.status_code == 200, ok.text
    assert over.status_code == 422, over.text
