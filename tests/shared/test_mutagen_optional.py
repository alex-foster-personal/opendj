"""Tag writing into audio files is refused. The product does not import mutagen.

mutagen is GPL-2.0-or-later. Reads stay on tinytag
(``tests/shared/test_tagreader_optional.py``).
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from apps.shared.tag_writer import TAG_WRITE_REMOVED, TagWriteRemoved, UnifiedTags


def test_modules_import_without_mutagen():
    for name in (
        "apps.shared.tag_writer",
        "apps.adapters.serato.geob",
        "apps.analysis.write_tags",
    ):
        mod = importlib.import_module(name)
        assert mod is not None
    import apps.shared.tag_writer as tag_writer

    assert not hasattr(tag_writer, "HAS_MUTAGEN")


def test_serato_geob_read_returns_empty(tmp_path):
    from apps.adapters.serato import geob

    fake = tmp_path / "x.mp3"
    before = b"not-a-real-mp3"
    fake.write_bytes(before)
    bundle = geob.read_geob_frames(fake)
    assert bundle.markers2.cues == () or bundle.markers2.cues == []
    assert bundle.beatgrid.markers == ()
    assert bundle.opaque_frames == {}
    assert fake.read_bytes() == before


def test_serato_geob_write_refuses_without_touching_the_file(tmp_path):
    from apps.adapters.serato import geob
    from apps.adapters.serato.geob import BeatGrid

    fake = tmp_path / "x.mp3"
    before = b"not-a-real-mp3"
    fake.write_bytes(before)
    with pytest.raises(TagWriteRemoved) as excinfo:
        geob.write_geob_frames(fake, beatgrid=BeatGrid(markers=()))
    assert "GPL" in str(excinfo.value)
    assert fake.read_bytes() == before


def test_tag_writer_write_refuses_without_touching_the_file(tmp_path):
    from apps.shared import tag_writer

    fake = tmp_path / "x.mp3"
    before = b"not-a-real-mp3"
    fake.write_bytes(before)
    with pytest.raises(TagWriteRemoved, match="GPL"):
        tag_writer.write_tags(fake, UnifiedTags(bpm=120.0), dry_run=True)
    with pytest.raises(TagWriteRemoved):
        tag_writer.write_tags(fake, UnifiedTags(bpm=120.0), dry_run=False)
    assert fake.read_bytes() == before


def test_analysis_write_tags_cli_refuses(tmp_path):
    from apps.analysis import write_tags as wt

    fake = tmp_path / "x.mp3"
    before = b"not-a-real-mp3"
    fake.write_bytes(before)
    assert wt.main([str(fake)]) == 2
    with pytest.raises(TagWriteRemoved):
        wt._write_tags(fake, {"BPM": "120"})
    assert TAG_WRITE_REMOVED in str(TagWriteRemoved())
    assert fake.read_bytes() == before


def test_audio_files_read_metadata_returns_none_for_unparseable_file(tmp_path):
    from apps.shared import audio_files

    fake = tmp_path / "nothing.mp3"
    fake.write_bytes(b"")
    assert audio_files.read_metadata(fake) is None


def test_audio_files_read_metadata_reads_tags_with_tinytag():
    from apps.shared import audio_files

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup" / "src-v2.mp3"
    meta = audio_files.read_metadata(fixture)
    assert meta is not None
    assert (meta.title, meta.artist) == ("Source V2", "Fixture")
    assert meta.duration_s is not None and 2.9 < meta.duration_s < 3.2
    assert meta.sample_rate == 22050
    assert meta.bitrate_kbps == 160
