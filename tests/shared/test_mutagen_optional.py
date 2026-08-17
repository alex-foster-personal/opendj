"""Regression: every guarded callsite must survive a missing mutagen install.

Backstop for LIC-1 (dep-audit PR #43). The contract:

* Modules still import cleanly with mutagen absent.
* Public tag-write entry points raise :class:`ImportError` with an
  install hint so users self-serve ``pip install music-dj-tools[tags]``.
* Read-only / best-effort paths (metadata scan, Serato GEOB read,
  fingerprint bitrate probe, matcher ID3 probe) degrade to ``None`` or
  an empty bundle without exploding.

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
    # audio_files imported HAS_MUTAGEN by value, so patch its local too.
    import apps.shared.audio_files as af

    monkeypatch.setattr(af, "HAS_MUTAGEN", False)
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
        "apps.shared.audio_files",
        "apps.shared.tag_writer",
        "apps.shared.fingerprints",
        "apps.sync.matcher",
        "apps.adapters.serato.geob",
        "apps.analysis.write_tags",
    ):
        mod = importlib.import_module(name)
        assert mod is not None


def test_audio_files_read_metadata_returns_none_without_mutagen(tmp_path, no_mutagen):
    from apps.shared import audio_files

    fake = tmp_path / "nothing.mp3"
    fake.write_bytes(b"")
    assert audio_files.read_metadata(fake) is None


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


def test_fingerprint_bitrate_soft_fails_without_mutagen(tmp_path, no_mutagen, monkeypatch):
    # Simulate the ImportError at ``import mutagen`` inside _safe_bitrate.
    from apps.shared import fingerprints

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "mutagen":
            raise ImportError("simulated: mutagen not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    fake = tmp_path / "x.mp3"
    fake.write_bytes(b"")
    assert fingerprints._safe_bitrate(fake) is None


def test_matcher_read_id3_soft_fails_without_mutagen(tmp_path, no_mutagen, monkeypatch):
    # Same trick as fingerprints: simulate ``from mutagen import File``
    # raising so the caller hits its except branch.
    from apps.sync import matcher

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "mutagen":
            raise ImportError("simulated: mutagen not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    fake = tmp_path / "x.mp3"
    fake.write_bytes(b"")
    assert matcher._read_id3(fake) is None
