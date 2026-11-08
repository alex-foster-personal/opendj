"""DJIMPORT-10225: the M3U reader turns plain and extended M3U into absolute paths in file order.

[if] a relative line, an extended directive or a CRLF ending is read wrong [then] fail, [else stop].
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.adapters.m3u import parse_m3u, read_m3u_file

pytestmark = [pytest.mark.requirement("DJIMPORT-10225")]


def _native(posix: str) -> str:
    """``posix`` spelled the way this OS spells that absolute path (Windows adds the drive)."""
    return os.path.normpath(os.path.abspath(posix))


BASE = Path(_native("/music/sets"))


def test_plain_m3u_keeps_absolute_paths_in_file_order() -> None:
    """[if] plain M3U lines are reordered or altered [then] fail, [else stop]."""
    assert parse_m3u("/music/b.mp3\n/music/a.flac\n", BASE) == [_native("/music/b.mp3"), _native("/music/a.flac")]


def test_extended_m3u_skips_directives_comments_and_blank_lines() -> None:
    """[if] #EXTM3U, #EXTINF or a blank line becomes a track [then] fail, [else stop]."""
    text = "#EXTM3U\n#EXTINF:215,Artist - Title\n/music/a.mp3\n\n# a comment\n#EXTINF:180,Other\n/music/b.mp3\n"
    assert parse_m3u(text, BASE) == [_native("/music/a.mp3"), _native("/music/b.mp3")]


def test_relative_paths_resolve_against_the_files_folder() -> None:
    """[if] a relative line is not resolved against the playlist's folder [then] fail, [else stop]."""
    assert parse_m3u("tracks/a.mp3\n../b.mp3\n", BASE) == [_native("/music/sets/tracks/a.mp3"), _native("/music/b.mp3")]


def test_crlf_lf_bom_and_file_uris_are_accepted() -> None:
    """[if] CRLF, a BOM or a file:// URI changes the paths read [then] fail, [else stop]."""
    uri = Path(_native("/music/My Track.mp3")).as_uri()
    text = f"﻿#EXTM3U\r\n/music/a.mp3\r\n{uri}\r\n"
    assert parse_m3u(text, BASE) == [_native("/music/a.mp3"), _native("/music/My Track.mp3")]


def test_a_file_reads_relative_to_its_own_folder(tmp_path: Path) -> None:
    """[if] read_m3u_file resolves relative lines against the cwd [then] fail, [else stop]."""
    playlist = tmp_path / "set.m3u8"
    playlist.write_text("#EXTM3U\r\nsub/a.mp3\r\n", encoding="utf-8")
    assert read_m3u_file(playlist) == [str(tmp_path.resolve() / "sub" / "a.mp3")]


def test_a_relative_base_dir_is_refused() -> None:
    """[if] a relative base_dir is accepted [then] fail, [else stop]."""
    with pytest.raises(ValueError):
        parse_m3u("a.mp3\n", Path("relative"))
