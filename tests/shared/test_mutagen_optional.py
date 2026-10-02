"""Regression: every mutagen-guarded callsite must survive a missing mutagen install.

Backstop for LIC-1 (dep-audit PR #43). Since Thu 1 Oct 2026 mutagen backs
only the opt-in tag WRITE family (tag reads moved to tinytag, see
``tests/shared/test_tagreader_optional.py``). The contract:

* Modules still import cleanly with mutagen absent.
* Public tag-write entry points raise :class:`ImportError` with an
  install hint so users self-serve ``pip install music-dj-tools[tags]``.
* The Serato GEOB read, paired with its writer, degrades to an empty bundle.

Simulated by monkeypatching ``apps.shared._mutagen.HAS_MUTAGEN`` to
``False`` at runtime rather than uninstalling the real mutagen wheel.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def no_mutagen(monkeypatch):
    """Flip the HAS_MUTAGEN sentinel off across every dependent module."""
    import apps.shared._mutagen as gate

    monkeypatch.setattr(gate, "HAS_MUTAGEN", False)
    return gate


def test_require_raises_with_install_hint(no_mutagen):
    with pytest.raises(ImportError) as excinfo:
        no_mutagen.require()
    msg = str(excinfo.value)
    assert "mutagen" in msg
    assert "music-dj-tools[tags]" in msg


def test_modules_still_importable_without_mutagen(no_mutagen):
    # Even with the flag flipped, re-importing the callsite modules must
    # not raise. (They do their mutagen work lazily.)
    for name in (
        "apps.shared.tag_writer",
        "apps.adapters.serato.geob",
        "apps.analysis.write_tags",
    ):
        mod = importlib.import_module(name)
        assert mod is not None


def test_serato_geob_read_returns_empty_without_mutagen(tmp_path, no_mutagen):
    from apps.adapters.serato import geob

    fake = tmp_path / "x.mp3"
    fake.write_bytes(b"")
    bundle = geob.read_geob_frames(fake)
    # Empty SeratoGEOB: no markers, no beatgrid markers, no opaque frames.
    assert bundle.markers2.cues == () or bundle.markers2.cues == []
    assert bundle.beatgrid.markers == ()
    assert bundle.opaque_frames == {}


def test_serato_geob_write_raises_without_mutagen(tmp_path, no_mutagen):
    from apps.adapters.serato import geob
    from apps.adapters.serato.geob import BeatGrid

    fake = tmp_path / "x.mp3"
    fake.write_bytes(b"")
    with pytest.raises(ImportError) as excinfo:
        geob.write_geob_frames(fake, beatgrid=BeatGrid(markers=()))
    assert "music-dj-tools[tags]" in str(excinfo.value)


def test_tag_writer_read_raises_without_mutagen(tmp_path, no_mutagen):
    from apps.shared import tag_writer

    fake = tmp_path / "x.mp3"
    fake.write_bytes(b"")
    with pytest.raises(ImportError) as excinfo:
        tag_writer.read_tags(fake)
    assert "music-dj-tools[tags]" in str(excinfo.value)


def test_tag_writer_write_raises_without_mutagen(tmp_path, no_mutagen):
    from apps.shared import tag_writer

    fake = tmp_path / "x.mp3"
    fake.write_bytes(b"")
    with pytest.raises(ImportError) as excinfo:
        tag_writer.write_tags(fake, tag_writer.UnifiedTags(bpm=120.0), dry_run=True)
    assert "music-dj-tools[tags]" in str(excinfo.value)


def test_analysis_write_tags_raise_without_mutagen(tmp_path, no_mutagen):
    from apps.analysis import write_tags as wt

    fake = tmp_path / "x.mp3"
    fake.write_bytes(b"")
    with pytest.raises(ImportError):
        wt._read_current_tags(fake)
    with pytest.raises(ImportError):
        wt._write_tags(fake, {"BPM": "120"})


def test_audio_files_read_metadata_returns_none_for_unparseable_file_without_mutagen(
    tmp_path, no_mutagen
):
    from apps.shared import audio_files

    fake = tmp_path / "nothing.mp3"
    fake.write_bytes(b"")
    assert audio_files.read_metadata(fake) is None


def test_audio_files_read_metadata_reads_tags_with_tinytag_without_mutagen(no_mutagen):
    """The packaged app has no mutagen, so a folder import must still get its tags.

    Regression: before tinytag took over this path, every folder import in
    the packaged app came in with no title or artist (Silver check, Fri 2 Oct 2026).
    """
    from pathlib import Path

    from apps.shared import audio_files

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup" / "src-v2.mp3"
    meta = audio_files.read_metadata(fixture)
    assert meta is not None
    assert (meta.title, meta.artist) == ("Source V2", "Fixture")
    assert meta.duration_s is not None and 2.9 < meta.duration_s < 3.2
    assert meta.sample_rate == 22050
    assert meta.bitrate_kbps == 160
