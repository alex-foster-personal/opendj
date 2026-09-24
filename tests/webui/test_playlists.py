"""Playlists endpoint tests (CAT-05, GUARD-11)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track

from .conftest import _stub_rb_vendor


@pytest.mark.requirement("CAT-05")
def test_list_playlists(client):
    r = client.get("/api/v1/playlists")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    names = {p["name"] for p in items}
    assert names == {"Opener Set", "Peak Hour"}


@pytest.mark.requirement("CAT-05")
def test_list_playlists_without_vendor_id_skips_rekordbox_db(
    client, monkeypatch,
):
    def fail_if_called() -> dict[str, int]:
        raise AssertionError("playlist ordering must not be queried")

    monkeypatch.setattr(
        "apps.webui.server.routes.playlists.rb_vendor.playlist_order_index",
        fail_if_called,
    )

    response = client.get("/api/v1/playlists")

    assert response.status_code == 200
    rekordbox = next(
        playlist for playlist in response.json()
        if playlist["vendor"] == "rekordbox"
    )
    assert rekordbox["seq"] is None


@pytest.mark.requirement("GUARD-11")
def test_get_playlist_diff_never_names_tracks_outside_real_membership(client):
    """pl-001's real membership is track-003/track-005 (seed_backend). The
    diff must never assert ids that are not in that membership -- the
    retired fixture's stable-aaa..stable-fff are exactly such ids."""
    r = client.get("/api/v1/playlists/pl-001")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Opener Set"
    assert body["items"] == ["track-003", "track-005"]
    diff = body["diff"]
    assert "rb_only" in diff
    assert "conflicts" in diff
    real_membership = set(body["items"])
    served_ids = (
        set(diff["rb_only"]) | set(diff["djay_only"]) | set(diff["both"])
        | {c["stable_id"] for c in diff["conflicts"]}
    )
    assert served_ids <= real_membership


@pytest.mark.requirement("GUARD-11")
def test_get_playlist_diff_is_empty_for_an_empty_playlist(monkeypatch):
    """Proves the exact issue #775 repro: an empty playlist must not be
    told it has six synced tracks across three buckets and a conflict."""
    _stub_rb_vendor(monkeypatch)
    backend = InMemoryBackend()
    backend.seed_playlist(Playlist(
        playlist_id="pl-empty", name="Empty Playlist", vendor="rekordbox",
        items=[],
    ))
    app = create_app(
        backend=backend, bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, syncthing_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        r = c.get("/api/v1/playlists/pl-empty")
    assert r.status_code == 200
    assert r.json()["diff"] == {
        "rb_only": [], "djay_only": [], "both": [], "conflicts": [],
    }


@pytest.mark.requirement("CAT-05")
def test_get_playlist_404(client):
    r = client.get("/api/v1/playlists/missing")
    assert r.status_code == 404


# --- pin e0f3a90652a9: GET /playlists must not hydrate full Track rows ----
#
# [if] GET /playlists needs only .file_path per member for available_count
# [then] it calls get_file_paths_bulk, never get_tracks_bulk (the EAV pass
# that #1271 already avoided for /tracks), [else stop].


@pytest.mark.requirement("CAT-05")
def test_list_playlists_never_hydrates_full_tracks(client, monkeypatch):
    """Mutation-provable guard for the pin e0f3a90652a9 fix: available_count
    only ever reads ``.file_path``, so the route must use the narrow
    ``get_file_paths_bulk`` -- not ``get_tracks_bulk``, whose EAV pass
    hydrates fields the summary discards."""
    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "GET /playlists must not hydrate full Track rows via "
            "get_tracks_bulk -- use get_file_paths_bulk"
        )

    monkeypatch.setattr(InMemoryBackend, "get_tracks_bulk", fail_if_called)

    r = client.get("/api/v1/playlists")

    assert r.status_code == 200
    by_id = {p["playlist_id"]: p for p in r.json()}
    assert by_id["pl-001"]["available_count"] == 0  # seeded tracks have no file_path
    assert by_id["pl-001"]["track_count"] == 2


@pytest.mark.requirement("CAT-05")
def test_get_file_paths_bulk_omits_missing_and_never_touches_eav_fields(
    monkeypatch,
):
    """InMemoryBackend and SqliteBackend must agree on the narrow contract:
    present ids map to their .file_path (None allowed), absent ids are
    simply missing from the result -- same absence contract as
    get_tracks_bulk, just narrower per row."""
    backend = InMemoryBackend()
    backend.seed_track(Track(stable_id="track-001", file_path="/music/a.mp3"))
    backend.seed_track(Track(stable_id="track-002", file_path=None))

    result = backend.get_file_paths_bulk(["track-001", "track-002", "missing-id"])

    assert result == {"track-001": "/music/a.mp3", "track-002": None}


@pytest.mark.requirement("CAT-05")
def test_get_playlists_timing_at_scale(monkeypatch, tmp_path: Path):
    """Timing regression guard (pin e0f3a90652a9): GET /playlists must not
    regress back to an O(members) full-Track hydration, or reintroduce any
    other per-member cost of similar order. This test creates 2,000 files
    (one playlist, one vendor). The 5s ceiling is deliberately generous:
    CI hardware varies and this checks gross regression rather than a
    precise production latency budget. Constructed input is not real-library
    performance acceptance."""
    _stub_rb_vendor(monkeypatch)
    data_dir = tmp_path / "data"
    state_db_path = data_dir / "state" / "state.db"
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    from apps.adapters.rekordbox import config as rb_config

    monkeypatch.setattr(rb_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(rb_config, "STATE_DB", state_db_path)
    n = 2000
    backend = InMemoryBackend()
    items: list[str] = []
    paths: list[str] = []
    for i in range(n):
        sid = f"scale-track-{i:05d}"
        path = tmp_path / f"{sid}.mp3"
        path.write_bytes(b"\x00")
        paths.append(str(path))
        backend.seed_track(Track(stable_id=sid, file_path=str(path)))
        items.append(sid)
    backend.seed_playlist(Playlist(
        playlist_id="pl-scale", name="Scale Playlist", vendor="djay",
        items=items,
    ))
    from apps.shared.state import db as state_db
    from apps.shared.state import schema as state_schema
    from apps.webui.server.rb_vendor_pkg import path_index

    conn = state_db.open_rw(state_db_path)
    try:
        state_schema.apply_migrations(conn)
        namespace = path_index.resolver_namespace(data_dir)
        path_index.upsert_rows(
            conn, namespace, [(path, 1) for path in paths],
        )
        conn.commit()
    finally:
        conn.close()
    app = create_app(
        backend=backend, bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, syncthing_status_fn=lambda: None,
        state_db_path=str(state_db_path),
    )
    with TestClient(app) as c:
        t0 = time.perf_counter()
        r = c.get("/api/v1/playlists")
        elapsed = time.perf_counter() - t0

    assert r.status_code == 200
    scale_pl = next(p for p in r.json() if p["playlist_id"] == "pl-scale")
    assert scale_pl["track_count"] == n
    assert scale_pl["available_count"] == n
    assert elapsed < 5.0, (
        f"GET /playlists took {elapsed:.3f}s for {n} members; "
        "expected well under the 5s gross-regression ceiling"
    )


pytestmark = pytest.mark.rb_parity
