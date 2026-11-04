"""Folder-image and online-cover artwork sources (apps.shared.artwork_sources).

The online tests drive :func:`online_cover` through a recording HTTP double
that answers with MusicBrainz's real response shape; no request leaves the
machine. Images are real Pillow-encoded JPEG/PNG bytes.

Regression one-liners:
  - if cover.jpg beside the file is not found then broken
  - if a lone unrelated image in the folder is taken as the cover then broken
  - if an image whose bytes do not match its extension is served then broken
  - if a stranger's recording with the same title matches then broken
  - if a soundtrack or compilation cover beats the artist's own release then broken
  - if a found cover is fetched again on the next view then broken
  - if a miss is looked up again within a week then broken
  - if a network failure is remembered as a miss then broken
  - if an offline machine sends a lookup on every view then broken
  - if the second of a deck's two simultaneous views shows no cover then broken
  - if a view waits on a stuck lookup without bound then broken
  - if ODJ_ARTWORK_ONLINE=0 still sends a request then broken
  - if a cached miss does not say whether MusicBrainz knew the song at all then broken
  - if a title+artist-only track MusicBrainz lacks gets no store cover then broken
  - if a store hit by a different artist with the same title is accepted then broken
  - if "Asco" matches "Ascoltare" (artist substring, not whole words) then broken
  - if "X - Techno Remix" does not match the store's "X (Techno Remix)" then broken
  - if a store cover URL off the store's own CDN is fetched then broken
  - if a store outage is cached as a miss then broken
  - if a miss does not name why each source missed then broken
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


def _release(group: str, primary: str | None, secondary: list[str], credit: str,
             status: str | None = "Official") -> dict:
    return {
        "status": status,
        "artist-credit": [{"name": credit}],
        "release-group": {"id": group, "primary-type": primary, "secondary-types": secondary},
    }


def test_own_release_beats_soundtrack_and_compilation() -> None:
    """Shape of the real 'Too Close' answer: MusicBrainz lists the film first."""
    query = TrackQuery(artist="Alex Clare", title="Too Close", duration_ms=None)
    recording = {"title": "Too Close", "length": None, "artist-credit": [{"name": "Alex Clare"}]}
    payload = {"recordings": [
        {**recording, "releases": [
            _release("taken-2", "Album", ["Soundtrack"], "Nathaniel Mechaly"),
            _release("mega-hits", "Album", ["Compilation"], "Various Artists"),
        ]},
        {**recording, "releases": [
            _release("bravo", None, [], "Various Artists", status=None),
            _release("lateness", "Album", [], "Alex Clare"),
        ]},
    ]}
    assert art.matching_release_groups(payload, query) == [
        "lateness", "bravo", "taken-2", "mega-hits",
    ]


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


NO_STORE_HITS = {
    art.DEEZER_URL: (200, json.dumps({"data": []}).encode()),
    art.ITUNES_URL: (200, json.dumps({"results": []}).encode()),
}


def test_miss_is_remembered(tmp_path: Path) -> None:
    http = RecordingHttp({art.MB_URL: (200, json.dumps({"recordings": []}).encode()),
                          **NO_STORE_HITS})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert len(http.calls) == 3, "MusicBrainz, Deezer, iTunes once each, then the cached miss"
    assert not cache.has("sid")
    assert cache.miss_reason("sid") == (
        "musicbrainz: no matching recording (MusicBrainz has no recording with this title by "
        "this artist); deezer: no artist+title match in 0 results; "
        "itunes: no artist+title match in 0 results"
    )


@pytest.mark.requirement("LIBM-187")
def test_miss_reason_says_when_recordings_came_back_but_none_matched(tmp_path: Path) -> None:
    """[if] MusicBrainz answers with recordings that do not match [then] the miss reason says so, [else stop]."""
    stranger = {"recordings": [_recording("Glue", "Some Stranger", 269_000, ["rg-x"])]}
    http = RecordingHttp({art.MB_URL: (200, json.dumps(stranger).encode()), **NO_STORE_HITS})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert cache.miss_reason("sid") is not None
    assert "MusicBrainz returned 1 " in str(cache.miss_reason("sid"))


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


def test_two_simultaneous_views_both_get_the_cover(tmp_path: Path) -> None:
    """A deck asks for its header and gutter sizes at once: one lookup, two covers."""
    cover = _image("JPEG")
    release = threading.Event()

    class SlowHttp(RecordingHttp):
        def get(self, url: str) -> tuple[int, bytes]:
            release.wait(5)
            return super().get(url)

    http = SlowHttp({
        art.MB_URL: (200, MB_FOUND.encode()),
        art.CAA_URL.format(rg="rg-1"): (200, cover),
    })
    cache = ArtworkCache(tmp_path / "artwork-cache")
    results: list[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(art.online_cover(cache, "sid", QUERY, http)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.1)  # both views are now in flight
    release.set()
    for thread in threads:
        thread.join(10)
    assert results == [(cover, "image/jpeg"), (cover, "image/jpeg")]
    assert len(http.calls) == 2, "one MusicBrainz and one CAA request, not two lookups"


def test_wait_on_a_stuck_lookup_is_bounded_and_caches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(art, "INFLIGHT_WAIT_S", 0.05)
    http = RecordingHttp({art.MB_URL: (200, MB_FOUND.encode())})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    with art._INFLIGHT_GUARD:
        lock = art._INFLIGHT.setdefault("sid", threading.Lock())
    lock.acquire()
    try:
        started = time.monotonic()
        assert art.online_cover(cache, "sid", QUERY, http) is None
        assert time.monotonic() - started < 1.0
    finally:
        lock.release()
    assert http.calls == []
    assert not cache.recent_miss("sid", 0.0) and not cache.has("sid")


# ----- store search (LIBM-188) --------------------------------------------------

DEEZER_COVER = "https://e-cdns-images.dzcdn.net/images/cover/abc/500x500-000000-80-0-0.jpg"
ITUNES_COVER_100 = "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/aa/cover.jpg/100x100bb.jpg"
MB_EMPTY = (200, json.dumps({"recordings": []}).encode())


def _deezer(*items: tuple[str, str, int, str]) -> tuple[int, bytes]:
    """Deezer /search answer shape (trimmed): title, artist.name, duration s, album.cover_big."""
    return 200, json.dumps({"data": [
        {"title": t, "artist": {"name": a}, "duration": d, "album": {"cover_big": c}}
        for t, a, d, c in items
    ]}).encode()


def _itunes(*items: tuple[str, str, int, str]) -> tuple[int, bytes]:
    """iTunes Search answer shape (trimmed): trackName, artistName, trackTimeMillis, artworkUrl100."""
    return 200, json.dumps({"results": [
        {"trackName": t, "artistName": a, "trackTimeMillis": ms, "artworkUrl100": c}
        for t, a, ms, c in items
    ]}).encode()


PHANTOM = TrackQuery(artist="Charles B, MOR3L, Amero",
                     title="The Phantom of the Opera - Techno Remix", duration_ms=167_915)


@pytest.mark.requirement("LIBM-188")
def test_store_hit_is_a_suggestion_never_a_cached_cover(tmp_path: Path) -> None:
    """Shape of Viva - The cut's Phantom remix: no MusicBrainz recording, on Deezer.

    [if] only a store (Deezer) has the cover [then] it is kept as a pending suggestion and nothing is fetched or cached, [else stop].
    """
    http = RecordingHttp({
        art.MB_URL: MB_EMPTY,
        art.DEEZER_URL: _deezer(
            ("O Fortuna", "Charles B", 168, DEEZER_COVER + "?wrong"),
            ("The Phantom of the Opera (Techno Remix)", "Charles B", 152, DEEZER_COVER),
            ("Phantom of the Opera Overture", "Charles B. Ancheta", 229, DEEZER_COVER + "?x"),
        ),
    })
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "sid", PHANTOM, http) is None
    assert DEEZER_COVER not in http.calls, "no store image is fetched without consent"
    assert not any(url.startswith(art.ITUNES_URL) for url in http.calls)
    assert not list((tmp_path / "artwork-cache").glob("*.img")), "nothing from a store is cached"
    suggestion = cache.suggestion("sid")
    assert suggestion is not None
    assert (suggestion.source, suggestion.cover_url) == ("deezer", DEEZER_COVER)
    assert set(cache.pending_suggestions()) == {"sid"}
    assert art.online_cover(cache, "sid", PHANTOM, http) is None
    assert len(http.calls) == 2, "a pending suggestion is not searched again on the next view"


@pytest.mark.requirement("LIBM-188")
def test_accepting_a_suggestion_fetches_and_caches_the_cover(tmp_path: Path) -> None:
    """[if] the user accepts a store suggestion [then] the cover is fetched and cached with its store source, [else stop]."""
    cover = _image("JPEG")
    http = RecordingHttp({art.MB_URL: MB_EMPTY, art.DEEZER_URL: _deezer(
        ("The Phantom of the Opera (Techno Remix)", "Charles B", 152, DEEZER_COVER)),
        DEEZER_COVER: (200, cover)})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    art.online_cover(cache, "sid", PHANTOM, http)
    art.accept_suggestion(cache, "sid", http)
    assert cache.get("sid") == (cover, "image/jpeg")
    assert cache.pending_suggestions() == {}
    meta = json.loads(next((tmp_path / "artwork-cache").glob("*.json")).read_bytes())
    assert (meta["source"], meta["ref"]) == ("deezer", DEEZER_COVER)


@pytest.mark.requirement("LIBM-188")
def test_skipping_a_suggestion_is_remembered_for_good(tmp_path: Path) -> None:
    """[if] the user skips a store suggestion [then] the track is never suggested again, even after a rerun, [else stop]."""
    http = RecordingHttp({art.MB_URL: MB_EMPTY, art.DEEZER_URL: _deezer(
        ("The Phantom of the Opera (Techno Remix)", "Charles B", 152, DEEZER_COVER))})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    art.online_cover(cache, "sid", PHANTOM, http)
    art.skip_suggestion(cache, "sid")
    assert cache.pending_suggestions() == {} and not cache.has("sid")
    cache.forget_miss("sid")  # a rerun forgets the miss, never the user's "no"
    art.online_cover(cache, "sid", PHANTOM, http)
    assert cache.suggestion("sid") is None
    assert "you skipped a store cover" in str(cache.miss_reason("sid"))
    assert sum(u.startswith(art.DEEZER_URL) for u in http.calls) == 1
    with pytest.raises(art.SuggestionError):
        art.accept_suggestion(cache, "sid", http)


@pytest.mark.requirement("LIBM-188")
def test_itunes_fills_what_deezer_lacks_and_suggests_600px(tmp_path: Path) -> None:
    """Shape of Vik Portos 'Capriccio D'inverno': Deezer empty, iTunes has the single.

    [if] Deezer has no match and iTunes does [then] the iTunes cover is suggested at 600px, [else stop].
    """
    big = ITUNES_COVER_100.replace("/100x100bb.", "/600x600bb.")
    http = RecordingHttp({
        art.MB_URL: MB_EMPTY,
        art.DEEZER_URL: _deezer(),
        art.ITUNES_URL: _itunes(("Capriccio D'inverno", "Vik Portos", 209_000, ITUNES_COVER_100)),
    })
    cache = ArtworkCache(tmp_path / "artwork-cache")
    query = TrackQuery(artist="Vik Portos", title="Capriccio D'inverno", duration_ms=209_345)
    assert art.online_cover(cache, "sid", query, http) is None
    suggestion = cache.suggestion("sid")
    assert suggestion is not None and (suggestion.source, suggestion.cover_url) == ("itunes", big)


@pytest.mark.requirement("LIBM-188")
def test_store_hit_needs_the_same_artist_and_title() -> None:
    """[if] a store hit differs in artist or title [then] it is not a match, and matches sort by closest length, [else stop]."""
    query = TrackQuery(artist="ASCO", title="Aria", duration_ms=207_438)
    hits = [
        art.StoreHit("Aria", "Someone Else", 207, DEEZER_COVER + "?1"),
        art.StoreHit("Aria", "Ascoltare", 207, DEEZER_COVER + "?2"),
        art.StoreHit("Aria (Live)", "ASCO", 207, DEEZER_COVER + "?3"),
        art.StoreHit("Aria", "Asco", 264, DEEZER_COVER + "?far"),
        art.StoreHit("Aria", "Asco, DJ Friend", 206, DEEZER_COVER + "?near"),
    ]
    assert [h.cover_url for h in art.matching_store_hits(hits, query)] == [
        DEEZER_COVER + "?near", DEEZER_COVER + "?far",
    ], "artist as whole words, exact title, closest length first, length not a veto"


@pytest.mark.requirement("LIBM-188")
def test_store_cover_off_the_store_cdn_is_never_fetched() -> None:
    """[if] a store hit's cover is off the store's own HTTPS CDN [then] it is never a match, [else stop]."""
    query = TrackQuery(artist="ASCO", title="Aria", duration_ms=None)
    hits = [
        art.StoreHit("Aria", "ASCO", None, "http://e-cdns-images.dzcdn.net/plain-http.jpg"),
        art.StoreHit("Aria", "ASCO", None, "https://evil.example/dzcdn.net.jpg"),
        art.StoreHit("Aria", "ASCO", None, "https://dzcdn.net.evil.example/a.jpg"),
        art.StoreHit("Aria", "ASCO", None, ""),
    ]
    assert art.matching_store_hits(hits, query) == []


@pytest.mark.requirement("LIBM-188")
def test_store_outage_is_not_cached_as_a_miss(tmp_path: Path) -> None:
    """[if] a store answers with an outage [then] no miss is cached and a later view retries, [else stop]."""
    http = RecordingHttp({art.MB_URL: MB_EMPTY, art.DEEZER_URL: (503, b"")})
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert cache.miss_reason("sid") is None and not cache.recent_miss("sid", 0.0)
    assert art.online_cover(cache, "sid", QUERY, http) is None
    assert len(http.calls) == 4, "a later view retries after a store outage"


@pytest.mark.requirement("LIBM-188")
def test_musicbrainz_hit_never_asks_the_stores(tmp_path: Path) -> None:
    """[if] MusicBrainz has the cover [then] neither Deezer nor iTunes is asked, [else stop]."""
    cover = _image("JPEG")
    http = RecordingHttp({
        art.MB_URL: (200, MB_FOUND.encode()),
        art.CAA_URL.format(rg="rg-1"): (200, cover),
    })
    cache = ArtworkCache(tmp_path / "artwork-cache")
    assert art.online_cover(cache, "sid", QUERY, http) == (cover, "image/jpeg")
    assert not any(u.startswith((art.DEEZER_URL, art.ITUNES_URL)) for u in http.calls)
