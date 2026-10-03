"""The listing's ``lyrics_available`` flag never fails a page (LIBM-137, round 4).

[if] the lyrics flag raises, counts a symlink, or lists the cache directory [then] fail, [else stop].

Regression one-liners:
  - if a lyrics-cache path that is a file, or unreadable, raises out of the flag then broken
  - if a symlinked cache entry counts as lyrics then broken
  - if an id that cannot be one file name raises, or reaches outside the cache directory, then broken
  - if the flag reads the whole cache directory then broken
  - if the flag and the lyrics read disagree about letter case on this volume then broken
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.lyrics import cache as lyrics_cache
from apps.webui.server.routes import tracks
from tests.platform_capabilities import posix_permission_denial_supported

pytestmark = [pytest.mark.requirement("LIBM-137")]


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    directory = lyrics_cache.cache_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "has-lyrics.json").write_text("{}", encoding="utf-8")
    return directory


def flags(data_dir: Path, *ids: str) -> dict[str, bool]:
    return tracks._lyrics_available_bulk(data_dir, list(ids))


def test_an_entry_that_is_a_plain_file_counts(cache: Path, tmp_path: Path) -> None:
    assert flags(tmp_path, "has-lyrics", "no-lyrics") == {"has-lyrics": True, "no-lyrics": False}


def test_a_cache_path_that_is_a_file_is_no_lyrics_for_anyone(tmp_path: Path) -> None:
    path = lyrics_cache.cache_dir(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not a directory", encoding="utf-8")
    assert flags(tmp_path, "a", "b") == {"a": False, "b": False}
    assert flags(tmp_path, "a") == {"a": False}


def test_a_missing_cache_directory_is_no_lyrics(tmp_path: Path) -> None:
    assert flags(tmp_path, "a") == {"a": False}


@pytest.mark.skipif(
    not posix_permission_denial_supported(os.name, getattr(os, "geteuid", None)),
    reason="needs POSIX permission bits and a non-root user (Windows has no os.geteuid; root ignores chmod)",
)
def test_an_unreadable_cache_directory_is_no_lyrics(cache: Path, tmp_path: Path) -> None:
    os.chmod(cache, 0)
    try:
        assert flags(tmp_path, "has-lyrics", "other") == {"has-lyrics": False, "other": False}
    finally:
        os.chmod(cache, 0o755)


def test_a_symlinked_entry_is_no_lyrics(cache: Path, tmp_path: Path) -> None:
    os.symlink(cache / "has-lyrics.json", cache / "linked.json")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    os.symlink(outside, cache / "escapes.json")
    assert flags(tmp_path, "linked", "escapes", "has-lyrics") == {
        "linked": False, "escapes": False, "has-lyrics": True,
    }


def test_a_directory_named_like_an_entry_is_no_lyrics(cache: Path, tmp_path: Path) -> None:
    (cache / "folder.json").mkdir()
    assert flags(tmp_path, "folder") == {"folder": False}


@pytest.mark.parametrize("stable_id", ["../has-lyrics", "sub/has-lyrics", "a\x00b", "\ud800", ""])
def test_an_id_that_is_not_one_file_name_is_no_lyrics(
    cache: Path, tmp_path: Path, stable_id: str
) -> None:
    (cache.parent / "has-lyrics.json").write_text("{}", encoding="utf-8")
    (cache / "sub").mkdir()
    (cache / "sub" / "has-lyrics.json").write_text("{}", encoding="utf-8")
    (cache / ".json").write_text("{}", encoding="utf-8")
    assert flags(tmp_path, stable_id) == {stable_id: False}


def test_the_flag_never_reads_the_directory(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the lyrics flag listed the cache directory")

    monkeypatch.setattr(os, "scandir", refuse)
    monkeypatch.setattr(os, "listdir", refuse)
    assert flags(tmp_path, "has-lyrics") == {"has-lyrics": True}
    assert flags(tmp_path, "has-lyrics", "no-lyrics")["has-lyrics"] is True


def test_letter_case_follows_the_volume_as_the_lyrics_read_does(cache: Path, tmp_path: Path) -> None:
    """Decision: the flag asks the volume for ``<id>.json`` by name, which is
    the lookup the lyrics read itself makes, so the two cannot disagree."""
    the_read_finds_it = lyrics_cache.cache_path(tmp_path, "HAS-LYRICS").is_file()
    assert flags(tmp_path, "HAS-LYRICS") == {"HAS-LYRICS": the_read_finds_it}
