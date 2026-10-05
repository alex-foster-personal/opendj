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
    assert empty == {"total": 0, "tracks": [], "offset": 0, "next_offset": None}


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
            "total": 0, "tracks": [], "offset": 0, "next_offset": None,
        }
        summary = c.get("/api/v1/reconcile/summary").json()
    # availability is None, not zeros: an in-memory backend has no state.db
    # to scan, and "unknown" must not read as "nothing is broken".
    assert isinstance(summary.pop("computed_at"), float)
    assert summary == {"total_tracks": 0, "total_broken": 0,
                       "orphan_broken": 0, "playlists": [],
                       "availability": None, "age_s": 0.0,
                       "refreshing": False, "refresh_error": None}


# --- summary scan: counts without whole Track rows (HEALTH-11) ---------------------


@pytest.fixture
def sqlite_backend(tmp_path: Path, library: dict[str, Path]):
    """A real state.db behind the real SqliteBackend: two present, two gone,
    one streaming, one pathless, and one soft-deleted row whose file is gone."""
    import sqlite3

    from apps.webui.server.sqlite_backend import SqliteBackend
    from tests.health_lights import fixtures as fx

    state_db = fx.make_state_db(tmp_path / "data")
    for stable_id, file_path in (
        ("t-ok", str(library["present"])),
        ("t-ok2", str(library["present2"])),
        ("t-gone", str(library["gone"])),
        ("t-gone2", str(library["gone2"])),
        ("t-stream", "tidal:12345"),
        ("t-nopath", None),
        ("t-deleted", str(library["gone"].with_name("deleted.mp3"))),
    ):
        fx.seed_track(state_db, stable_id, file_path)
    conn = sqlite3.connect(state_db)
    conn.execute("UPDATE tracks SET deleted_at = ? WHERE stable_id = 't-deleted'", (fx.STAMP,))
    conn.commit()
    conn.close()
    return SqliteBackend(state_db)


@pytest.mark.requirement("HEALTH-11")
def test_summary_scan_matches_the_full_scan_on_a_real_state_db(sqlite_backend) -> None:
    """[if] the lean summary scan and the full /broken scan read the same
    library [then] they agree on the total and on every broken id."""
    total, broken = reconcile_routes._scan_broken(sqlite_backend)
    lean_total, lean_ids = reconcile_routes._scan_broken_ids(sqlite_backend)

    assert {b.track.stable_id for b in broken} == {"t-gone", "t-gone2"}    # the control fires
    assert lean_ids == {"t-gone", "t-gone2"}
    assert lean_total == total == 6          # the soft-deleted row is in neither


@pytest.mark.requirement("HEALTH-11")
def test_summary_does_not_read_whole_track_rows(
    sqlite_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] /reconcile/summary is asked [then] it never calls list_tracks:
    that read resolved every analysis field per track and took seconds."""
    calls: list[tuple] = []
    real = sqlite_backend.list_tracks

    def counting(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlite_backend, "list_tracks", counting)
    app = create_app(backend=sqlite_backend, bind_host="127.0.0.1", hostname="test-host",
                     lock_status_fn=lambda: None, mount_frontend=False)
    app.include_router(reconcile_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        body = c.get("/api/v1/reconcile/summary").json()
        assert body["total_tracks"] == 6 and body["total_broken"] == 2
        assert calls == []
    # Control: the full scan DOES go through list_tracks, so the counter works.
    reconcile_routes._scan_broken(sqlite_backend)
    assert calls != []


@pytest.mark.requirement("HEALTH-11")
def test_summary_scan_falls_back_for_a_backend_without_a_state_db(
    backend: InMemoryBackend,
) -> None:
    total, ids = reconcile_routes._scan_broken_ids(backend)
    assert (total, ids) == (5, {"t-gone", "t-gone2"})


# --- /broken: paged, and a page costs a page (LIBM-136) ----------------------------


@pytest.mark.requirement("LIBM-136")
def test_broken_pages_cover_the_listing_once_in_order(client: TestClient) -> None:
    """[if] /broken is read one row at a time [then] the pages are the unpaged
    listing, in order, with `total` the whole count on every page."""
    whole = client.get("/api/v1/reconcile/broken").json()
    assert [t["stable_id"] for t in whole["tracks"]] == ["t-gone2", "t-gone"]
    assert (whole["offset"], whole["next_offset"]) == (0, None)

    first = client.get("/api/v1/reconcile/broken", params={"limit": 1}).json()
    assert first["total"] == 2
    assert (first["offset"], first["next_offset"]) == (0, 1)
    second = client.get(
        "/api/v1/reconcile/broken", params={"limit": 1, "offset": first["next_offset"]}
    ).json()
    assert second["total"] == 2
    assert (second["offset"], second["next_offset"]) == (1, None)
    assert first["tracks"] + second["tracks"] == whole["tracks"]


@pytest.mark.requirement("LIBM-136")
def test_broken_page_past_the_end_is_empty_not_an_error(client: TestClient) -> None:
    body = client.get("/api/v1/reconcile/broken", params={"limit": 5, "offset": 9}).json()
    assert body == {"total": 2, "tracks": [], "offset": 9, "next_offset": None}


@pytest.mark.requirement("LIBM-136")
@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 1001}, {"offset": -1}, {"limit": "many"}]
)
def test_broken_rejects_a_page_it_cannot_serve(client: TestClient, params: dict) -> None:
    assert client.get("/api/v1/reconcile/broken", params=params).status_code == 422


@pytest.mark.requirement("LIBM-136")
def test_broken_playlist_filter_pages_within_the_playlist(client: TestClient) -> None:
    body = client.get(
        "/api/v1/reconcile/broken", params={"playlist_id": "pl-1", "limit": 1}
    ).json()
    assert body["total"] == 1
    assert [t["stable_id"] for t in body["tracks"]] == ["t-gone"]
    assert body["next_offset"] is None


@pytest.mark.requirement("LIBM-136")
def test_a_broken_page_reads_whole_track_rows_for_that_page_only(
    sqlite_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] one page of /broken is asked on a real state.db [then] whole track
    rows are read for that page's ids only, and never through list_tracks:
    the full-library drain is what made the page take seconds."""
    listed: list[tuple] = []
    hydrated: list[list[str]] = []
    real_list, real_bulk = sqlite_backend.list_tracks, sqlite_backend.get_tracks_bulk

    def counting_list(*args, **kwargs):
        listed.append((args, kwargs))
        return real_list(*args, **kwargs)

    def counting_bulk(stable_ids):
        hydrated.append(list(stable_ids))
        return real_bulk(stable_ids)

    # The oracle, taken BEFORE the counters go on: the old full scan's rows.
    _total, full = reconcile_routes._scan_broken(sqlite_backend)
    oracle = [
        reconcile_routes._to_row(b, []).model_dump() for b in full
    ]
    assert [row["stable_id"] for row in oracle] == ["t-gone", "t-gone2"]

    monkeypatch.setattr(sqlite_backend, "list_tracks", counting_list)
    monkeypatch.setattr(sqlite_backend, "get_tracks_bulk", counting_bulk)
    app = create_app(backend=sqlite_backend, bind_host="127.0.0.1", hostname="test-host",
                     lock_status_fn=lambda: None, mount_frontend=False)
    app.include_router(reconcile_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        first = c.get("/api/v1/reconcile/broken", params={"limit": 1}).json()
        assert hydrated == [["t-gone"]]
        second = c.get("/api/v1/reconcile/broken", params={"limit": 1, "offset": 1}).json()
        assert hydrated == [["t-gone"], ["t-gone2"]]
    assert listed == []
    assert first["total"] == second["total"] == 2
    # Overshoot control: paging must not change what any row says.
    assert first["tracks"] + second["tracks"] == oracle
