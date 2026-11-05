"""Paginated playlist tracks route (PERF-UI-05, issue #3530)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track
from apps.webui.server.sqlite_backend import SqliteBackend

from .app_fixtures import _stub_rb_vendor

# Min-of-N is the least load-sensitive estimate on a shared machine (timeit's advice; issue #7449).
_WARM_TIMING_PASSES = 5
_WARM_BUDGET_S = 0.5


def _best_warmed_get(request_fn, *, passes: int = _WARM_TIMING_PASSES):
    samples: list[float] = []
    response = None
    for _ in range(passes):
        t0 = time.perf_counter()
        response = request_fn()
        samples.append(time.perf_counter() - t0)
        assert response.status_code == 200, response.text
    assert response is not None
    return response, min(samples), samples


def _seed_1k_playlist(backend: InMemoryBackend, tmp_path: Path) -> str:
    items: list[str] = []
    for i in range(1000):
        sid = f"pl-perf-track-{i:04d}"
        path = tmp_path / f"{sid}.mp3"
        path.write_bytes(b"\x00")
        backend.seed_track(Track(stable_id=sid, file_path=str(path), title=f"Track {i}"))
        items.append(sid)
    backend.seed_playlist(
        Playlist(
            playlist_id="pl-perf-1k",
            name="Perf 1k",
            vendor="djay",
            items=items,
        )
    )
    return "pl-perf-1k"


@pytest.mark.requirement("PERF-UI-05")
def test_list_playlist_tracks_first_page_has_etag_and_slice(monkeypatch, tmp_path: Path):
    """[if] offset 0 limit 100 [then] first page returns 100 rows and ETag, [else stop]."""
    _stub_rb_vendor(monkeypatch)
    backend = InMemoryBackend()
    playlist_id = _seed_1k_playlist(backend, tmp_path)
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        r = c.get(f"/api/v1/playlists/{playlist_id}/tracks", params={"limit": 100, "offset": 0})
    assert r.status_code == 200
    assert r.headers.get("etag")
    body = r.json()
    assert body["total"] == 1000
    assert len(body["tracks"]) == 100
    assert body["next_offset"] == 100
    assert body["tracks"][0]["stable_id"] == "pl-perf-track-0000"


@pytest.mark.requirement("PERF-UI-05")
def test_list_playlist_tracks_second_page(monkeypatch, tmp_path: Path):
    """[if] offset 100 [then] next slice continues membership order, [else stop]."""
    _stub_rb_vendor(monkeypatch)
    backend = InMemoryBackend()
    playlist_id = _seed_1k_playlist(backend, tmp_path)
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        r = c.get(f"/api/v1/playlists/{playlist_id}/tracks", params={"limit": 100, "offset": 100})
    assert r.status_code == 200
    body = r.json()
    assert len(body["tracks"]) == 100
    assert body["tracks"][0]["stable_id"] == "pl-perf-track-0100"
    assert body["next_offset"] == 200


def _seed_1k_state_db(tmp_path: Path) -> tuple[Path, str]:
    """A real state.db written through StateWriter, members backed by real files."""
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        stable_ids: list[str] = []
        for i in range(1000):
            sid = f"pl-perf-track-{i:04d}"
            path = tmp_path / f"{sid}.mp3"
            path.write_bytes(b"\x00")
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=f"Track {i}",
                artists=[f"Artist {i}"],
                album=None,
                isrc=None,
                duration_ms=60_000,
                file_path=str(path),
            )
            stable_ids.append(sid)
        writer.insert_playlist(
            playlist_id="pl-perf-1k",
            name="Perf 1k",
            vendor="fixture",
            vendor_pl_id="pl-perf-1k",
        )
        writer.set_playlist_memberships("pl-perf-1k", stable_ids)
    finally:
        writer.close()
        conn.close()
    return db_path, "pl-perf-1k"


@pytest.mark.requirement("PERF-UI-05")
def test_list_playlist_tracks_first_page_is_fast(tmp_path: Path):
    """[if] warmed offset-0 limit-30 GET on a real state.db [then] under 0.5s, [else stop].

    Real storage and hydration end to end: SqliteBackend over a StateWriter-
    seeded state.db, no vendor stub, members on disk so availability is a
    real stat pass. The rows asserted below prove the timed request went
    through that path rather than returning early.
    """
    db_path, playlist_id = _seed_1k_state_db(tmp_path)
    backend = SqliteBackend(db_path)
    assert not isinstance(backend, InMemoryBackend)
    app = create_app(
        backend=backend,
        state_db_path=str(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        warm = c.get(
            f"/api/v1/playlists/{playlist_id}/tracks",
            params={"limit": 30, "offset": 0},
        )
        assert warm.status_code == 200, warm.text
        r, elapsed, samples = _best_warmed_get(
            lambda: c.get(
                f"/api/v1/playlists/{playlist_id}/tracks",
                params={"limit": 30, "offset": 0},
            ),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1000
    assert [t["stable_id"] for t in body["tracks"]] == [f"pl-perf-track-{i:04d}" for i in range(30)]
    assert body["tracks"][7]["title"] == "Track 7"
    assert elapsed < _WARM_BUDGET_S, (
        f"best of {_WARM_TIMING_PASSES} warmed playlist tracks page took {elapsed:.3f}s "
        f"(samples={[round(s, 3) for s in samples]}) on a real state.db; "
        f"first-page slice must stay under {_WARM_BUDGET_S}s once TestClient startup is paid"
    )


@pytest.mark.requirement("PERF-UI-05")
def test_list_playlists_availability_skip_is_fast(monkeypatch, tmp_path: Path):
    """[if] availability=skip [then] no get_file_paths_bulk and -1 counts, [else stop]."""
    _stub_rb_vendor(monkeypatch)
    backend = InMemoryBackend()
    _seed_1k_playlist(backend, tmp_path)

    def _fail_paths_bulk(*_args, **_kwargs):
        raise AssertionError("fast list must not call get_file_paths_bulk")

    monkeypatch.setattr(InMemoryBackend, "get_file_paths_bulk", _fail_paths_bulk)
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        # First GET pays TestClient/FastAPI startup; time the skip path itself.
        warm = c.get("/api/v1/playlists", params={"availability": "skip"})
        assert warm.status_code == 200
        r, elapsed, samples = _best_warmed_get(
            lambda: c.get("/api/v1/playlists", params={"availability": "skip"}),
        )
    assert r.status_code == 200
    pl = next(p for p in r.json() if p["playlist_id"] == "pl-perf-1k")
    assert pl["available_count"] == -1
    assert pl["track_count"] == 1000
    assert elapsed < _WARM_BUDGET_S, (
        f"best of {_WARM_TIMING_PASSES} warmed availability=skip took {elapsed:.3f}s "
        f"(samples={[round(s, 3) for s in samples]}); "
        f"skip must stay under {_WARM_BUDGET_S}s once TestClient startup is paid"
    )
