"""Tests for apps.spotify.client -- fetch + cache + pagination + retry."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from apps.spotify.client import MissingCredentialsError, SpotifyClient

from .fake_spotipy import FakeSpotipy, make_meta, make_track


@pytest.fixture
def fake_cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    return d


@pytest.mark.requirement("CAT-01")
def test_from_env_raises_when_client_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    with pytest.raises(MissingCredentialsError):
        SpotifyClient.from_env()


@pytest.mark.requirement("CAT-01")
def test_fetch_single_page(fake_cache_dir: Path) -> None:
    fake = FakeSpotipy(
        meta=make_meta(),
        items_pages=[[make_track(spotify_id="trk1"), make_track(spotify_id="trk2")]],
    )
    client = SpotifyClient(fake, cache_dir=fake_cache_dir)
    playlist = client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=False)

    assert playlist.id == "37i9dQZF1DXcBWIGoYBM5M"
    assert playlist.track_count == 2
    assert playlist.tracks[0].spotify_id == "trk1"
    assert playlist.tracks[0].isrc == "USABC2500001"


@pytest.mark.requirement("CAT-01")
def test_fetch_paginates_over_multiple_pages(fake_cache_dir: Path) -> None:
    page1 = [make_track(spotify_id=f"t{i:03d}") for i in range(100)]
    page2 = [make_track(spotify_id=f"t{i:03d}") for i in range(100, 200)]
    page3 = [make_track(spotify_id=f"t{i:03d}") for i in range(200, 250)]
    fake = FakeSpotipy(meta=make_meta(), items_pages=[page1, page2, page3])

    client = SpotifyClient(fake, cache_dir=fake_cache_dir)
    playlist = client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=False)

    assert playlist.track_count == 250
    kinds = [op for op, _ in fake.call_log]
    assert kinds == ["playlist", "playlist_items", "playlist_items", "playlist_items"]


@pytest.mark.requirement("CAT-01")
def test_fetch_writes_cache_and_reuses_it(fake_cache_dir: Path) -> None:
    fake = FakeSpotipy(
        meta=make_meta(),
        items_pages=[[make_track(spotify_id="t1")]],
    )
    client = SpotifyClient(fake, cache_dir=fake_cache_dir)
    client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=True)
    assert len(fake.call_log) == 2
    client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=True)
    assert len(fake.call_log) == 2  # served from cache
    client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=False)
    assert len(fake.call_log) == 4


@pytest.mark.requirement("CAT-01")
def test_fetch_handles_null_track_entry(fake_cache_dir: Path) -> None:
    fake = FakeSpotipy(
        meta=make_meta(),
        items_pages=[[{"track": None}, make_track(spotify_id="t1")]],
    )
    client = SpotifyClient(fake, cache_dir=fake_cache_dir)
    playlist = client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=False)
    assert playlist.track_count == 1


@pytest.mark.requirement("CAT-01")
def test_fetch_local_track_has_no_isrc(fake_cache_dir: Path) -> None:
    fake = FakeSpotipy(
        meta=make_meta(),
        items_pages=[[make_track(spotify_id=None, isrc=None, is_local=True)]],
    )
    client = SpotifyClient(fake, cache_dir=fake_cache_dir)
    playlist = client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=False)
    t = playlist.tracks[0]
    assert t.is_local is True
    assert t.isrc is None


@pytest.mark.requirement("CAT-01")
def test_fetch_retries_once_on_401(fake_cache_dir: Path) -> None:
    fake = FakeSpotipy(
        meta=make_meta(),
        items_pages=[[make_track(spotify_id="t1")]],
        raise_401_once_on="playlist",
    )
    client = SpotifyClient(fake, cache_dir=fake_cache_dir)
    playlist = client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=False)
    assert playlist.track_count == 1
    kinds = [op for op, _ in fake.call_log]
    assert kinds.count("playlist") == 2


@pytest.mark.requirement("CAT-01")
def test_cache_freshness_honours_ttl(fake_cache_dir: Path) -> None:
    fake = FakeSpotipy(meta=make_meta(), items_pages=[[make_track(spotify_id="t1")]])
    client = SpotifyClient(fake, cache_dir=fake_cache_dir, cache_ttl=1)
    client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=True)
    cache_file = fake_cache_dir / "37i9dQZF1DXcBWIGoYBM5M.json"
    past = time.time() - 7200
    os.utime(cache_file, (past, past))
    before = len(fake.call_log)
    client.fetch_playlist("37i9dQZF1DXcBWIGoYBM5M", use_cache=True)
    assert len(fake.call_log) > before
