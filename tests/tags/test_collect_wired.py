"""Tests for the real default RB / djay / MIK collectors wired in Phase 7 gap-fill.

Covers:
  * RB default builds a path-indexed TagRead lookup from the working-copy DB.
  * djay default builds the same from ``MediaLibrary.db`` by local path.
  * MIK default returns None and logs a single warning (Phase 6 unshipped).
  * Callable injection still wins over the default fetcher.
  * Collector swallows exceptions raised by custom fetchers.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.shared.tag_writer import TagRead
from apps.tags import collect as collect_mod
from apps.tags.collect import (
    _reset_caches_for_tests,
    collect_for,
    default_fetch_djay,
    default_fetch_mik,
    default_fetch_rb,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


@pytest.fixture(autouse=True)
def _clear_collect_caches() -> Iterator[None]:
    _reset_caches_for_tests()
    try:
        yield
    finally:
        _reset_caches_for_tests()


# ---------------------------------------------------------- RB default


def _fake_rb_track(**overrides):
    """Build a minimal ``RBTrack``-shaped namespace for iter_tracks stubs."""
    base = dict(
        id="1",
        title="RB Title",
        artist="RB Artist",
        album="RB Album",
        genre="Techno",
        folder_path="/Music/A.mp3",
        file_path=Path("/Music/A.mp3"),
        is_streaming=False,
        bpm=128.0,
        rating=4,
        file_size=1024,
        date_added=None,
        isrc="USRC12345678",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.requirement("META-03")
def test_default_fetch_rb_hits_and_miss(monkeypatch) -> None:
    """When the shim returns a track, default_fetch_rb finds it by path;
    missing paths return None."""

    class _FakeRekordboxDB:
        pass

    def _fake_open_db():
        return _FakeRekordboxDB()

    def _fake_iter(_db):
        yield _fake_rb_track()
        yield _fake_rb_track(
            folder_path="/Music/B.mp3",
            file_path=Path("/Music/B.mp3"),
            title="B",
            artist="B-art",
            isrc=None,
        )

    from apps.shared import rekordbox_db as real_mod

    monkeypatch.setattr(real_mod, "open_db", _fake_open_db)
    monkeypatch.setattr(real_mod, "iter_tracks", _fake_iter)

    hit = default_fetch_rb(Path("/Music/A.mp3"))
    assert hit is not None
    assert hit.title == "RB Title"
    assert hit.genre == "Techno"
    assert hit.isrc == "USRC12345678"
    assert hit.bpm == pytest.approx(128.0)

    miss = default_fetch_rb(Path("/Music/Nope.mp3"))
    assert miss is None


@pytest.mark.requirement("META-03")
def test_default_fetch_rb_skips_streaming(monkeypatch) -> None:
    """Streaming rows must not land in the index."""
    from apps.shared import rekordbox_db as real_mod

    def _iter(_db):
        yield _fake_rb_track(
            folder_path="spotify:track:123",
            file_path=None,
            is_streaming=True,
        )

    monkeypatch.setattr(real_mod, "open_db", lambda: object())
    monkeypatch.setattr(real_mod, "iter_tracks", _iter)

    assert default_fetch_rb(Path("spotify:track:123")) is None


@pytest.mark.requirement("META-03")
def test_default_fetch_rb_returns_none_when_db_missing(monkeypatch) -> None:
    """If open_db raises, default_fetch_rb returns None and marks unavailable."""
    from apps.shared import rekordbox_db as real_mod

    def _raise():
        raise FileNotFoundError("no live RB db")

    monkeypatch.setattr(real_mod, "open_db", _raise)
    assert default_fetch_rb(Path("/anything.mp3")) is None
    # A second call should stay cheap (cache marks unavailable).
    assert default_fetch_rb(Path("/anything.mp3")) is None


# --------------------------------------------------------- djay default


def _fake_djay_track(**overrides):
    base = dict(
        uuid="uuid-1",
        title="djay Title",
        artist="djay Artist",
        isrc="",
        source_uri="file:///Music/A.mp3",
        file_path=Path("/Music/A.mp3"),
        is_local=True,
        rating=3,
        duration_s=180.0,
        play_count=2,
        color_index=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.requirement("META-03")
def test_default_fetch_djay_hits_by_local_path(monkeypatch, tmp_path) -> None:
    """default_fetch_djay builds an index keyed by local file path."""
    from apps.shared import djay_db as real_djay
    from apps.shared import paths as shared_paths

    # Point DJAY_LIVE_DB at an existing file so the existence check passes.
    fake_db = tmp_path / "MediaLibrary.db"
    fake_db.write_bytes(b"SQLite format 3\x00")
    monkeypatch.setattr(shared_paths, "DJAY_LIVE_DB", fake_db)

    def _iter(_path):
        yield _fake_djay_track()
        # A streaming track must not land in the index.
        yield _fake_djay_track(
            uuid="uuid-2",
            source_uri="spotify:track:abc",
            file_path=None,
            is_local=False,
        )

    monkeypatch.setattr(real_djay, "iter_tracks", _iter)

    hit = default_fetch_djay(Path("/Music/A.mp3"))
    assert hit is not None
    assert hit.title == "djay Title"
    assert hit.rating == 3

    assert default_fetch_djay(Path("/Music/Missing.mp3")) is None


@pytest.mark.requirement("META-03")
def test_default_fetch_djay_returns_none_when_db_absent(monkeypatch, tmp_path) -> None:
    from apps.shared import paths as shared_paths

    monkeypatch.setattr(shared_paths, "DJAY_LIVE_DB", tmp_path / "does-not-exist.db")
    assert default_fetch_djay(Path("/Music/A.mp3")) is None


# ---------------------------------------------------------- MIK default


@pytest.mark.requirement("META-03")
def test_default_fetch_mik_returns_none_and_warns_once(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="apps.tags.collect")

    assert default_fetch_mik(Path("/Music/A.mp3")) is None
    assert default_fetch_mik(Path("/Music/B.mp3")) is None

    mik_warnings = [r for r in caplog.records if "MIK fetcher" in r.getMessage()]
    assert len(mik_warnings) == 1, "MIK warning should log exactly once per process"


# ------------------------------------------------------- collect_for wiring


@pytest.mark.requirement("META-03")
def test_collect_for_uses_defaults_by_default(monkeypatch, tmp_path) -> None:
    """No explicit fetchers -> collect_for calls our default_* functions."""
    calls: dict[str, int] = {"rb": 0, "djay": 0, "mik": 0}

    def _fake_rb(p):
        calls["rb"] += 1
        return TagRead(title="from-rb")

    def _fake_djay(p):
        calls["djay"] += 1
        return TagRead(title="from-djay")

    def _fake_mik(p):
        calls["mik"] += 1
        return None

    monkeypatch.setattr(collect_mod, "default_fetch_rb", _fake_rb)
    monkeypatch.setattr(collect_mod, "default_fetch_djay", _fake_djay)
    monkeypatch.setattr(collect_mod, "default_fetch_mik", _fake_mik)

    target = tmp_path / "Artist - Title.mp3"
    target.write_bytes(b"\x00" * 16)  # unreadable as tags, read_tags returns None

    sources = collect_for(target)
    assert calls == {"rb": 1, "djay": 1, "mik": 1}
    assert sources.rb is not None and sources.rb.title == "from-rb"
    assert sources.djay is not None and sources.djay.title == "from-djay"
    assert sources.mik is None


@pytest.mark.requirement("META-03")
def test_collect_for_injected_callable_overrides_default(monkeypatch, tmp_path) -> None:
    """Explicit fetch_rb=... bypasses the default path."""

    def _unused(_p):
        raise AssertionError("default fetcher should not be called when injected")

    monkeypatch.setattr(collect_mod, "default_fetch_rb", _unused)

    target = tmp_path / "track.mp3"
    target.write_bytes(b"\x00" * 16)

    sentinel = TagRead(title="injected")
    sources = collect_for(target, fetch_rb=lambda _p: sentinel)
    assert sources.rb is sentinel


@pytest.mark.requirement("META-03")
def test_collect_for_swallows_fetcher_exceptions(tmp_path) -> None:
    """A misbehaving injected fetcher must not bubble out."""
    target = tmp_path / "track.mp3"
    target.write_bytes(b"\x00" * 16)

    def _boom(_p):
        raise RuntimeError("boom")

    sources = collect_for(
        target,
        fetch_rb=_boom,
        fetch_djay=_boom,
        fetch_mik=_boom,
    )
    assert sources.rb is None
    assert sources.djay is None
    assert sources.mik is None
