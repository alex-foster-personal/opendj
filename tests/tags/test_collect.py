"""Tests for ``apps.tags.collect``."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.shared.tag_writer import TagRead
from apps.tags.collect import collect_for, parse_filename

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


@pytest.mark.requirement("META-03")
def test_parse_filename_artist_title() -> None:
    out = parse_filename(Path("/a/b/Cobalt Row - Night Ferry.mp3"))
    assert out.artist == "Cobalt Row"
    assert out.title == "Night Ferry"


@pytest.mark.requirement("META-03")
def test_parse_filename_unparseable_empty() -> None:
    out = parse_filename(Path("/a/b/track1.mp3"))
    assert out.artist is None
    assert out.title is None


@pytest.mark.requirement("META-03")
def test_collect_reads_file_and_filename(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)
    sources = collect_for(dst)
    assert sources.filename.artist == "Artist"
    assert sources.filename.title == "Title"
    assert sources.file is not None
    # RB / djay / MIK default to None.
    assert sources.rb is None
    assert sources.djay is None
    assert sources.mik is None


@pytest.mark.requirement("META-03")
def test_collect_accepts_injected_fetchers(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "file.mp3"
    shutil.copy2(src, dst)

    rb_hit = TagRead(title="RB Title", genre="Techno")
    sources = collect_for(dst, fetch_rb=lambda p: rb_hit)
    assert sources.rb is rb_hit


@pytest.mark.requirement("META-03")
def test_zero_rating_preserved_in_collect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_djay_build_index` must preserve ``rating == 0`` instead of None-ing it.

    Regression for codex finding P07-04: ``x if x else None`` treated
    0 as falsy and erased valid zero-star ratings. The fix is to use
    ``x if x is not None else None``.
    """
    from types import SimpleNamespace

    from apps.shared import djay_db
    from apps.shared import paths as shared_paths
    from apps.tags import collect as tags_collect

    # _djay_build_index requires the configured live DB path to exist.
    fake_db = tmp_path / "djay_live.db"
    fake_db.write_bytes(b"")
    monkeypatch.setattr(shared_paths, "DJAY_LIVE_DB", fake_db, raising=False)

    def _fake_iter_tracks(_path):
        yield SimpleNamespace(
            is_local=True,
            file_path=Path("/music/zero.mp3"),
            title="Zero",
            artist="Z",
            isrc=None,
            rating=0,
        )
        yield SimpleNamespace(
            is_local=True,
            file_path=Path("/music/three.mp3"),
            title="Three",
            artist="T",
            isrc=None,
            rating=3,
        )

    monkeypatch.setattr(djay_db, "iter_tracks", _fake_iter_tracks)

    index = tags_collect._djay_build_index()
    assert index is not None

    zero_key = tags_collect._rb_normalise_path("/music/zero.mp3")
    three_key = tags_collect._rb_normalise_path("/music/three.mp3")

    # rating=0 is a real value and must be preserved (not erased to None).
    assert index[zero_key].rating == 0
    assert index[three_key].rating == 3

