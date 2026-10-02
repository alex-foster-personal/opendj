"""Contract tests for the read-only reconcile router (LANE reconcile-router).

Requirements (node missing-tracks-folder, data side):

✔︎ ✅ GET /api/v1/reconcile/broken lists exactly the local tracks whose
      recorded path is missing on disk (streaming + pathless excluded).
✔︎ ✅ GET /api/v1/reconcile/broken?playlist_id= filters to one playlist and
      404s on an unknown playlist.
✔︎ ✅ GET /api/v1/reconcile/summary reports total/orphan broken counts and
      per-playlist track_count/broken_count (zero counts included).
✔︎ ✅ rekordbox FolderPath (via bulk_rb_meta) wins over the state-layer
      file_path and surfaces vendor_id.

Acceptance tests:

* [if] a non-streaming track's file_path does not exist [then ⛔️] it appears
  in /broken with original_path/basename/parent_dir and file_exists false.
* [if] a track is streaming or has no path [then ⛔️] it never appears.
* [if] ?playlist_id names an unknown playlist [then ⛔️] 404, not [].
* [if] a broken track sits in two playlists [then ⛔️] both playlist_ids are
  listed and both playlists' broken_count increment.
* [if] a broken track sits in no playlist [then ⛔️] orphan_broken counts it.
* [if] bulk_rb_meta maps a track to a dead rekordbox FolderPath while the
  state file_path exists [then ⛔️] the track is broken with that vendor_id.

Uses InMemoryBackend + real tmp files (disk truth, no mocked stat results);
rb_vendor.bulk_rb_meta is monkeypatched to {} so tests never depend on the
developer's state.db / master.plain.db (the one rb-meta test supplies an
explicit mapping instead).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server import rb_vendor
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track
from apps.webui.server.routes import reconcile as reconcile_routes


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Two real files on disk; two recorded paths that are gone."""
    present = tmp_path / "present.mp3"
    present.write_bytes(b"ID3fake")
    present2 = tmp_path / "Manual Library" / "present2.aiff"
    present2.parent.mkdir()
    present2.write_bytes(b"FORMfake")
    monkeypatch.setattr(rb_vendor, "bulk_rb_meta", lambda stable_ids: {})
    return {
        "present": present,
        "present2": present2,
        "gone": tmp_path / "Convert temp 2 (BACKUP)" / "gone.mp3",
        "gone2": tmp_path / "gone2.wav",
    }


@pytest.fixture
def backend(library: dict[str, Path]) -> InMemoryBackend:
    b = InMemoryBackend()
    b.seed_track(Track(stable_id="t-ok", title="Fine", artist="A",
                       file_path=str(library["present"])))
    b.seed_track(Track(stable_id="t-gone", title="Vanished", artist="B",
                       bpm=124.0, key="8A", rating=4, duration_ms=200_000,
                       file_path=str(library["gone"])))
    b.seed_track(Track(stable_id="t-gone2", title="Also Gone", artist="C",
                       file_path=str(library["gone2"])))
    b.seed_track(Track(stable_id="t-stream", title="Streamy", artist="D",
                       file_path="tidal:12345"))
    b.seed_track(Track(stable_id="t-nopath", title="Pathless", artist="E",
                       file_path=None))
    b.seed_playlist(Playlist(playlist_id="pl-1", name="Warmup",
                             vendor="rekordbox",
                             items=["t-ok", "t-gone"]))
    b.seed_playlist(Playlist(playlist_id="pl-2", name="Peak",
                             vendor="djay",
                             items=["t-gone", "t-stream"]))
    b.seed_playlist(Playlist(playlist_id="pl-3", name="Empty", vendor="djay",
                             items=[]))
    return b


@pytest.fixture
def client(backend: InMemoryBackend) -> Iterator[TestClient]:
    app = create_app(backend=backend, bind_host="127.0.0.1",
                     hostname="test-host", lock_status_fn=lambda: None,
                     mount_frontend=False)
    # Integrator wiring under test -- same one-liner app.py will get.
    app.include_router(reconcile_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        yield c


@pytest.mark.requirement("RECON-01")
def test_broken_lists_only_missing_local_tracks(
    client: TestClient, library: dict[str, Path],
) -> None:
    r = client.get("/api/v1/reconcile/broken")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    by_id = {t["stable_id"]: t for t in body["tracks"]}
    assert set(by_id) == {"t-gone", "t-gone2"}
    # sorted by title: "Also Gone" before "Vanished"
    assert [t["stable_id"] for t in body["tracks"]] == ["t-gone2", "t-gone"]

    gone = by_id["t-gone"]
    assert gone["original_path"] == str(library["gone"])
    assert gone["basename"] == "gone.mp3"
    assert gone["parent_dir"] == str(library["gone"].parent)
    assert gone["bpm"] == 124.0
    assert gone["duration_ms"] == 200_000
    assert gone["file_exists"] is False
    assert gone["is_streaming"] is False
    assert gone["vendor_id"] is None
    assert gone["playlist_ids"] == ["pl-1", "pl-2"]
    assert by_id["t-gone2"]["playlist_ids"] == []


@pytest.mark.requirement("RECON-01")
def test_broken_excludes_streaming_and_pathless(client: TestClient) -> None:
    body = client.get("/api/v1/reconcile/broken").json()
    ids = {t["stable_id"] for t in body["tracks"]}
    assert "t-stream" not in ids
    assert "t-nopath" not in ids
    assert "t-ok" not in ids


@pytest.mark.requirement("RECON-01")
def test_broken_playlist_filter(client: TestClient) -> None:
    body = client.get("/api/v1/reconcile/broken",
                      params={"playlist_id": "pl-1"}).json()
    assert body["total"] == 1
    assert body["tracks"][0]["stable_id"] == "t-gone"

    empty = client.get("/api/v1/reconcile/broken",
                       params={"playlist_id": "pl-3"}).json()
    assert empty == {"total": 0, "tracks": []}


@pytest.mark.requirement("RECON-01")
def test_broken_unknown_playlist_404(client: TestClient) -> None:
    r = client.get("/api/v1/reconcile/broken",
                   params={"playlist_id": "nope"})
    assert r.status_code == 404


@pytest.mark.requirement("RECON-01")
def test_summary_counts(client: TestClient) -> None:
    r = client.get("/api/v1/reconcile/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["total_tracks"] == 5
    assert body["total_broken"] == 2
    assert body["orphan_broken"] == 1  # t-gone2 is in no playlist
    per = {p["playlist_id"]: p for p in body["playlists"]}
    assert set(per) == {"pl-1", "pl-2", "pl-3"}
    assert per["pl-1"] == {"playlist_id": "pl-1", "name": "Warmup",
                           "vendor": "rekordbox", "track_count": 2,
                           "broken_count": 1}
    assert per["pl-2"]["broken_count"] == 1
    assert per["pl-2"]["track_count"] == 2
    assert per["pl-3"]["broken_count"] == 0
    # broken-heavy playlists sort first, zero-broken alphabetical after
    assert [p["broken_count"] for p in body["playlists"]] == [1, 1, 0]


@pytest.mark.requirement("RECON-01")
def test_rb_folder_path_wins_over_state_path(
    backend: InMemoryBackend, library: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # t-ok's state file_path exists, but its rekordbox FolderPath is dead:
    # rekordbox truth wins (same resolution order as rb_vendor row hydration)
    # and vendor_id surfaces for relocate-files.
    dead_rb_path = str(library["gone"].parent / "moved-elsewhere.mp3")
    meta = rb_vendor.RbRowMeta(vendor_id="42", folder_path=dead_rb_path,
                               analysis_data_path=None, comment=None,
                               genre=None, play_count=0)
    monkeypatch.setattr(
        rb_vendor, "bulk_rb_meta",
        lambda stable_ids: {"t-ok": meta} if "t-ok" in stable_ids else {},
    )
    app = create_app(backend=backend, bind_host="127.0.0.1",
                     hostname="test-host", lock_status_fn=lambda: None,
                     mount_frontend=False)
    app.include_router(reconcile_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        body = c.get("/api/v1/reconcile/broken").json()
    by_id = {t["stable_id"]: t for t in body["tracks"]}
    assert "t-ok" in by_id
    assert by_id["t-ok"]["vendor_id"] == "42"
    assert by_id["t-ok"]["original_path"] == dead_rb_path
    assert body["total"] == 3


@pytest.mark.requirement("RECON-01")
def test_empty_library(tmp_path: Path,
                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rb_vendor, "bulk_rb_meta", lambda stable_ids: {})
    app = create_app(backend=InMemoryBackend(), bind_host="127.0.0.1",
                     hostname="test-host", lock_status_fn=lambda: None,
                     mount_frontend=False)
    app.include_router(reconcile_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        assert c.get("/api/v1/reconcile/broken").json() == {
            "total": 0, "tracks": [],
        }
        summary = c.get("/api/v1/reconcile/summary").json()
    # availability is None, not zeros: an in-memory backend has no state.db
    # to scan, and "unknown" must not read as "nothing is broken".
    assert summary == {"total_tracks": 0, "total_broken": 0,
                       "orphan_broken": 0, "playlists": [],
                       "availability": None}
