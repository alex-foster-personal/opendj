"""Folder-image and online-cover artwork sources (apps.shared.artwork_sources).

The online tests drive :func:`online_cover` through a recording HTTP double
that answers with MusicBrainz's real response shape; no request leaves the
machine. Images are real Pillow-encoded JPEG/PNG bytes.

Regression one-liners:
  - if cover.jpg beside the file is not found then broken
  - if a lone unrelated image in the folder is taken as the cover then broken
  - if an image whose bytes do not match its extension is served then broken
  - if a stranger's recording with the same title matches then broken
  - if a found cover is fetched again on the next view then broken
  - if a miss is looked up again within a week then broken
  - if a network failure is remembered as a miss then broken
  - if an offline machine sends a lookup on every view then broken
  - if a second view waits on a lookup already running then broken
  - if ODJ_ARTWORK_ONLINE=0 still sends a request then broken
"""
from __future__ import annotations

import json
import os
import threading
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from apps.shared import artwork_sources as art
from apps.shared.artwork_sources import ArtworkCache, TrackQuery


def _image(fmt: str, color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (16, 16), color).save(out, format=fmt)
    return out.getvalue()


def _recording(title: str, artist: str, length_ms: int | None, groups: list[str]) -> dict:
    return {
        "title": title,
        "length": length_ms,
        "artist-credit": [{"name": artist}],
        "releases": [{"id": f"rel-{g}", "release-group": {"id": g}} for g in groups],
    }


class RecordingHttp:
    """Answers GETs from a URL-prefix table and records every request."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                return answer
        return 404, b""


@pytest.fixture(autouse=True)
def _online_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODJ_ARTWORK_ONLINE", "1")
    monkeypatch.setattr(art._BACKOFF, "until", 0.0)


# ----- folder image -----------------------------------------------------------


def test_sidecar_prefers_cover_over_folder(tmp_path: Path) -> None:
    track = tmp_path / "a.mp3"
    track.write_bytes(b"x")
    cover = _image("JPEG", (1, 2, 3))
    (tmp_path / "Cover.JPG").write_bytes(cover)
    (tmp_path / "folder.png").write_bytes(_image("PNG"))
    assert art.sidecar_artwork(track) == (cover, "image/jpeg")
    assert art.sidecar_artwork_path(track) == tmp_path / "Cover.JPG"


def test_sidecar_ignores_a_lone_unrelated_image(tmp_path: Path) -> None:
    track = tmp_path / "a.mp3"
    track.write_bytes(b"x")
    (tmp_path / "screenshot.jpg").write_bytes(_image("JPEG"))
    assert art.sidecar_artwork(track) is None
    assert art.sidecar_artwork_path(track) is None


def test_sidecar_rejects_bytes_that_do_not_match_the_extension(tmp_path: Path) -> None:
    track = tmp_path / "a.mp3"
    track.write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(_image("PNG"))
    assert art.sidecar_artwork(track) is None


def test_sidecar_memo_sees_a_cover_added_later(tmp_path: Path) -> None:
    track = tmp_path / "a.mp3"
    track.write_bytes(b"x")
    assert art.sidecar_artwork_path(track) is None
    (tmp_path / "cover.jpg").write_bytes(_image("JPEG"))
    # A new entry moves the directory mtime; force a distinct stamp on coarse clocks.
    stamp = tmp_path.stat().st_mtime_ns + 10_000_000
    os.utime(tmp_path, ns=(stamp, stamp))
    assert art.sidecar_artwork_path(track) == tmp_path / "cover.jpg"


# ----- matching ---------------------------------------------------------------


def test_matching_requires_title_artist_and_duration() -> None:
    query = TrackQuery(artist="Bicep, Clara La San", title="6A - 7 - Glue (Original Mix)",
                       duration_ms=269_000)
    payload = {"recordings": [
        _recording("Glue", "Some Stranger", 269_000, ["stranger"]),
        _recording("Glue", "Bicep", 400_000, ["too-long"]),
        _recording("Glue", "Bicep", 270_500, ["rg-1", "rg-2"]),
        _recording("Glue", "Bicep", None, ["rg-2", "rg-3"]),
    ]}
    assert art.matching_release_groups(payload, query) == ["rg-1", "rg-2", "rg-3"]


def test_query_cleaning() -> None:
    assert art.query_title("6A - 7 - Glue") == "Glue"
    assert art.query_title("E - Mergency") == "E - Mergency"
    assert art.query_artist("Anyma, Chris Avantgarde") == "Anyma"
    assert art.query_artist("Apashe feat. Black Prez") == "Apashe"


# ----- online cover -----------------------------------------------------------

QUERY = TrackQuery(artist="Bicep", title="Glue", duration_ms=269_000)
MB_FOUND = json.dumps({"recordings": [_recording("Glue", "Bicep", 269_000, ["rg-1", "rg-2"])]})


def test_found_cover_is_cached_and_not_fetched_again(tmp_path: Path) -> None:
    cover = _image("JPEG")
    http = RecordingHttp({
        art.MB_URL: (200, MB_FOUND.encode()),
        art.CAA_URL.format(rg="rg-1"): (404, b""),
        art.CAA_URL.format(rg="rg-2"): (200, cover),
    })
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "sid", QUERY, http) == (cover, "image/jpeg")
    assert len(http.calls) == 3
    assert cache.has("sid")
    assert art.online_cover(cache, "sid", QUERY, http) == (cover, "image/jpeg")
    assert len(http.calls) == 3, "a cached cover must not be looked up again"


def test_miss_is_remembered(tmp_path: Path) -> None:
    http = RecordingHttp({art.MB_URL: (200, json.dumps({"recordings": []}).encode())})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert len(http.calls) == 1
    assert not cache.has("sid")


def test_network_failure_is_not_remembered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = RecordingHttp({art.MB_URL: (art.NETWORK_FAILURE, b"")})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert not cache.recent_miss("sid", 0.0) and not cache.has("sid")
    monkeypatch.setattr(art._BACKOFF, "until", 0.0)  # the backoff has passed
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert len(http.calls) == 2, "an unreachable service must be retried on a later view"


def test_offline_backs_off_instead_of_retrying_every_view(tmp_path: Path) -> None:
    http = RecordingHttp({art.MB_URL: (art.NETWORK_FAILURE, b"")})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "a", QUERY, http) is None
    assert art.online_cover(cache, "a", QUERY, http) is None
    assert art.online_cover(cache, "b", QUERY, http) is None
    assert len(http.calls) == 1, "while offline no view may hold a thread on the network"


def test_busy_queue_is_not_offline(tmp_path: Path) -> None:
    http = RecordingHttp({art.MB_URL: (art.BUSY, b"")})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert len(http.calls) == 2, "a full queue is not a reason to stop trying"
    assert not cache.recent_miss("sid", 0.0)


def test_view_during_a_running_lookup_returns_at_once(tmp_path: Path) -> None:
    http = RecordingHttp({art.MB_URL: (200, MB_FOUND.encode())})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    with art._INFLIGHT_GUARD:
        lock = art._INFLIGHT.setdefault("sid", threading.Lock())
    lock.acquire()
    try:
        started = time.monotonic()
        assert art.online_cover(cache, "sid", QUERY, http) is None
        assert time.monotonic() - started < 0.2, "the second view waited on the first"
    finally:
        lock.release()
    assert http.calls == []


def test_disabled_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODJ_ARTWORK_ONLINE", "0")
    http = RecordingHttp({art.MB_URL: (200, MB_FOUND.encode())})
    assert art.online_cover(ArtworkCache(tmp_path), "sid", QUERY, http) is None
    assert http.calls == []


def test_no_artist_sends_nothing(tmp_path: Path) -> None:
    http = RecordingHttp({art.MB_URL: (200, MB_FOUND.encode())})
    query = TrackQuery(artist=None, title="Glue", duration_ms=None)
    assert art.online_cover(ArtworkCache(tmp_path), "sid", query, http) is None
    assert http.calls == []
