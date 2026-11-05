"""Playlist art thumbnails (ARTTHUMB-8728, ARTTHUMB-8728b, issue #8592)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from apps.webui.server import playlist_thumbnail as thumbs
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Playlist, Track

from .app_fixtures import _stub_rb_vendor

RED, GREEN, BLUE, YELLOW, WHITE = (220, 30, 30), (30, 200, 30), (30, 30, 220), (230, 220, 20), (250, 250, 250)


def _jpeg(rgb: tuple[int, int, int], px: int = 120) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (px, px), rgb).save(out, format="JPEG", quality=95)
    return out.getvalue()


def _reader(covers: dict[str, bytes | None]):
    calls: list[str] = []

    def read(stable_id: str) -> bytes | None:
        calls.append(stable_id)
        return covers.get(stable_id)

    return read, calls


def _pixel(data: bytes, x: int, y: int) -> tuple[int, int, int]:
    return Image.open(io.BytesIO(data)).convert("RGB").getpixel((x, y))


def _near(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - b) < 40 for a, b in zip(actual, expected, strict=True))


# ------------------------------------------------------------ composition ---


@pytest.mark.requirement("ARTTHUMB-8728")
def test_mosaic_uses_first_four_distinct_covers_in_order():
    """[if] five distinct covers with a repeat [then] a 2x2 of covers 1-4, the repeat skipped, [else stop]."""
    covers = {
        "a": _jpeg(RED),
        "b": _jpeg(RED),
        "c": _jpeg(GREEN),
        "d": None,
        "e": _jpeg(BLUE),
        "f": _jpeg(YELLOW),
        "g": _jpeg(WHITE),
    }
    read, _ = _reader(covers)
    comp = thumbs.choose_covers(list("abcdefg"), read)
    assert comp.kind == "mosaic"
    data = thumbs.render(comp, 64)
    assert data is not None
    assert Image.open(io.BytesIO(data)).size == (64, 64)
    assert _near(_pixel(data, 8, 8), RED)
    assert _near(_pixel(data, 56, 8), GREEN)
    assert _near(_pixel(data, 8, 56), BLUE)
    assert _near(_pixel(data, 56, 56), YELLOW)


@pytest.mark.requirement("ARTTHUMB-8728")
def test_one_album_and_few_covers_use_the_first_cover_alone():
    """[if] one album, or three distinct covers [then] the first cover fills the tile, [else stop]."""
    read, _ = _reader({"a": _jpeg(GREEN), "b": _jpeg(GREEN), "c": _jpeg(GREEN)})
    one_album = thumbs.choose_covers(["a", "b", "c"], read)
    assert one_album.kind == "single"
    read3, _ = _reader({"a": _jpeg(BLUE), "b": _jpeg(RED), "c": _jpeg(GREEN)})
    three = thumbs.render(thumbs.choose_covers(["a", "b", "c"], read3), 232)
    assert three is not None
    assert _near(_pixel(three, 10, 10), BLUE) and _near(_pixel(three, 220, 220), BLUE)


@pytest.mark.requirement("ARTTHUMB-8728")
def test_no_cover_and_empty_playlist_are_placeholders_never_images():
    """[if] no member has art, or the list is empty [then] placeholder, no image rendered, [else stop]."""
    read, _ = _reader({"a": None})
    assert thumbs.choose_covers(["a"], read).kind == "placeholder"
    assert thumbs.choose_covers([], read).kind == "placeholder"
    assert thumbs.render(thumbs.choose_covers([], read), 64) is None


# ------------------------------------------------------------------ cache ---


@pytest.mark.requirement("ARTTHUMB-8728")
def test_cache_hit_reads_no_cover_and_reorder_rebuilds(tmp_path: Path):
    """[if] same members twice [then] second call reads no cover; [if] reordered [then] new composite, [else stop]."""
    covers = {k: _jpeg(c) for k, c in zip("abcd", (RED, GREEN, BLUE, YELLOW), strict=True)}
    read, calls = _reader(covers)
    first, key1 = thumbs.cached_thumbnail(tmp_path, 64, list("abcd"), read)
    n = len(calls)
    second, key2 = thumbs.cached_thumbnail(tmp_path, 64, list("abcd"), read)
    assert first == second and key1 == key2 and len(calls) == n
    reordered, key3 = thumbs.cached_thumbnail(tmp_path, 64, list("dcba"), read)
    assert key3 != key1 and reordered is not None
    assert _near(_pixel(reordered, 8, 8), YELLOW)


@pytest.mark.requirement("ARTTHUMB-8728")
def test_placeholder_cache_expires_and_bad_size_refused(tmp_path: Path):
    """[if] a cached placeholder is older than the TTL [then] covers are re-read; [if] size 100 [then] refused, [else stop]."""
    read, calls = _reader({})
    clock = [1_000_000.0]
    thumbs.cached_thumbnail(tmp_path, 64, ["a"], read, now=lambda: clock[0])
    marker = next(tmp_path.glob("*.none"))
    import os

    os.utime(marker, (clock[0], clock[0]))
    thumbs.cached_thumbnail(tmp_path, 64, ["a"], read, now=lambda: clock[0] + 10)
    assert calls == ["a"], "a fresh placeholder must not re-read covers"
    thumbs.cached_thumbnail(tmp_path, 64, ["a"], read, now=lambda: clock[0] + thumbs.PLACEHOLDER_TTL_S + 1)
    assert calls == ["a", "a"], "an expired placeholder must re-read covers"
    with pytest.raises(ValueError):
        thumbs.cached_thumbnail(tmp_path, 100, ["a"], read)


# ------------------------------------------------------------------ route ---


def _client(monkeypatch, tmp_path: Path, covers: dict[str, bytes | None]) -> TestClient:
    _stub_rb_vendor(monkeypatch)
    monkeypatch.setattr(thumbs, "track_cover_bytes", covers.get)
    backend = InMemoryBackend()
    for sid in covers:
        backend.seed_track(Track(stable_id=sid, title=sid))
    backend.seed_playlist(Playlist(playlist_id="pl-1", name="One", vendor="djay", items=list(covers)))
    backend.seed_playlist(Playlist(playlist_id="pl-empty", name="Empty", vendor="djay", items=[]))
    (tmp_path / "state").mkdir()
    app = create_app(
        backend=backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        state_db_path=str(tmp_path / "state" / "state.db"),
    )
    return TestClient(app)


@pytest.mark.requirement("ARTTHUMB-8728b")
def test_route_serves_jpeg_then_304_and_caches_under_the_library_state_dir(monkeypatch, tmp_path: Path):
    """[if] a playlist with covers [then] image/jpeg, kind cover, cached on disk, 304 on its etag, [else stop]."""
    with _client(monkeypatch, tmp_path, {"a": _jpeg(RED), "b": _jpeg(BLUE)}) as c:
        r = c.get("/api/v1/playlists/pl-1/thumbnail", params={"size": 232})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["x-thumbnail-kind"] == "cover"
        assert "/artwork" not in str(r.url)
        assert Image.open(io.BytesIO(r.content)).size == (232, 232)
        assert list((tmp_path / "state" / thumbs.CACHE_DIR_NAME).glob("*.jpg"))
        again = c.get(
            "/api/v1/playlists/pl-1/thumbnail", params={"size": 232}, headers={"If-None-Match": r.headers["etag"]}
        )
        assert again.status_code == 304


@pytest.mark.requirement("ARTTHUMB-8728b")
def test_route_placeholder_bad_size_and_unknown_playlist(monkeypatch, tmp_path: Path):
    """[if] empty list [then] the SVG placeholder; [if] size 100 [then] 422; [if] unknown id [then] 404, [else stop]."""
    with _client(monkeypatch, tmp_path, {"a": None}) as c:
        r = c.get("/api/v1/playlists/pl-empty/thumbnail")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert r.headers["x-thumbnail-kind"] == "placeholder"
        assert c.get("/api/v1/playlists/pl-1/thumbnail", params={"size": 100}).status_code == 422
        assert c.get("/api/v1/playlists/nope/thumbnail").status_code == 404


# -------------------------------------------------------------------- CLI ---


@pytest.mark.requirement("ARTTHUMB-8728b")
def test_cli_targets_the_same_route_and_refuses_a_bad_size(capsys):
    """[if] opendj playlist thumbnail X [then] it asks the route; [if] --size 100 [then] usage failure, [else stop]."""
    from apps.opendj_cli import EXIT_FAILED, playlist_cli

    assert playlist_cli.thumbnail_path("pl 1/x", 232) == "/api/v1/playlists/pl%201%2Fx/thumbnail?size=232"
    assert playlist_cli.run(["thumbnail", "pl-1", "--size", "100"], as_json=True) == EXIT_FAILED
    assert "usage" in capsys.readouterr().out


@pytest.mark.requirement("ARTTHUMB-8728")
def test_concurrent_misses_on_one_key_all_serve_a_whole_image(tmp_path: Path):
    """[if] eight threads miss the same key at once [then] none fails and the cached file is a whole JPEG, [else stop]."""
    from concurrent.futures import ThreadPoolExecutor

    covers = {k: _jpeg(c) for k, c in zip("abcd", (RED, GREEN, BLUE, YELLOW), strict=True)}

    def _one(_: int) -> tuple[bytes | None, str]:
        return thumbs.cached_thumbnail(tmp_path, 232, list("abcd"), covers.get)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_one, range(8)))
    assert all(data is not None for data, _ in results)
    (cached,) = list(tmp_path.glob("*.jpg"))
    assert Image.open(cached).size == (232, 232)
    assert not list(tmp_path.glob("*.tmp"))
