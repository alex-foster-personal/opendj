"""End-to-end tests for apps.spotify.importer."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema
from apps.spotify.client import SpotifyClient
from apps.spotify.importer import run_import

from .fake_spotipy import FakeSpotipy, make_meta, make_track


@pytest.fixture
def state_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path, isolation_level=None)
    state_schema.apply_migrations(conn)
    conn.execute(
        """INSERT INTO tracks
           (stable_id, stable_id_tier, title, artists_json, album, isrc,
            duration_ms, file_path, content_hash, created_at, updated_at)
           VALUES ('s1', 'isrc', 'Hello', ?, 'A', 'USABC2500001', 200000,
                   '/x.mp3', NULL, '2026', '2026')""",
        (json.dumps(["Artist"]),),
    )
    conn.close()
    monkeypatch.setattr("apps.shared.paths.STATE_DB", path)
    monkeypatch.setattr("apps.shared.state.paths.STATE_DB", path)
    return path


@pytest.mark.requirement("CAT-01")
def test_run_import_dry_run(tmp_path: Path, state_db_path: Path) -> None:
    fake = FakeSpotipy(
        meta=make_meta(playlist_id="pl123"),
        items_pages=[[
            make_track(spotify_id="t1", isrc="USABC2500001",
                       name="Hello", artists=["Artist"]),
            make_track(spotify_id="t2", isrc="ZZ9999", name="Mystery"),
        ]],
    )
    client = SpotifyClient(fake, cache_dir=tmp_path / "cache")
    run = run_import(
        "pl123", client=client, live=False,
        state_db_path=state_db_path, out_root=tmp_path / "out",
        include_timestamp=False,
    )
    assert run.live is False
    assert run.write_summary is None
    assert len(run.result.matched) == 1
    assert len(run.result.unmatched) == 1
    for p in (run.reports.matches_csv, run.reports.to_acquire_csv,
              run.reports.to_acquire_md, run.reports.summary_md):
        assert p.exists()


@pytest.mark.requirement("CAT-01")
def test_run_import_live_writes_state(tmp_path: Path, state_db_path: Path) -> None:
    fake = FakeSpotipy(
        meta=make_meta(playlist_id="pl123", snapshot_id="snap-1"),
        items_pages=[[
            make_track(spotify_id="t1", isrc="USABC2500001",
                       name="Hello", artists=["Artist"]),
        ]],
    )
    client = SpotifyClient(fake, cache_dir=tmp_path / "cache")
    run = run_import(
        "pl123", client=client, live=True,
        state_db_path=state_db_path, out_root=tmp_path / "out",
        include_timestamp=False,
    )
    assert run.live is True
    assert run.write_summary is not None
    assert run.write_summary.matched_written == 1
    assert run.write_summary.backup_path.exists()
    assert run.write_summary.reversal_script_path.exists()

    conn = sqlite3.connect(state_db_path)
    rows = conn.execute(
        "SELECT vendor, vendor_pl_id FROM playlists WHERE vendor = 'spotify'"
    ).fetchall()
    conn.close()
    assert rows == [("spotify", "pl123")]


@pytest.mark.requirement("CAT-01")
def test_max_tracks_caps(tmp_path: Path, state_db_path: Path) -> None:
    fake = FakeSpotipy(
        meta=make_meta(playlist_id="pl123"),
        items_pages=[[
            make_track(spotify_id=f"t{i}", isrc=None, name=f"X{i}")
            for i in range(10)
        ]],
    )
    client = SpotifyClient(fake, cache_dir=tmp_path / "cache")
    run = run_import(
        "pl123", client=client, live=False, max_tracks=3,
        state_db_path=state_db_path, out_root=tmp_path / "out",
        include_timestamp=False,
    )
    assert len(run.result.pairs) == 3


@pytest.mark.requirement("CAT-01")
def test_missing_state_db_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "no.db"
    monkeypatch.setattr("apps.shared.paths.STATE_DB", missing)
    monkeypatch.setattr("apps.shared.state.paths.STATE_DB", missing)

    fake = FakeSpotipy(
        meta=make_meta(playlist_id="pl123"),
        items_pages=[[make_track(spotify_id="t1", isrc="X")]],
    )
    client = SpotifyClient(fake, cache_dir=tmp_path / "cache")
    run = run_import(
        "pl123", client=client, live=False,
        state_db_path=missing, out_root=tmp_path / "out",
        include_timestamp=False,
    )
    assert len(run.result.unmatched) == 1
