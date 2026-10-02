"""Regression: every tag READ callsite survives a missing tinytag install.

tinytag (MIT) is a core dependency since Thu 1 Oct 2026, replacing GPL
mutagen for reads (``apps/shared/_tagreader.py``). It is still guarded, so a
broken environment degrades to an explicit "reader unavailable" rather than
an import crash. The contract:

* Modules still import cleanly with the reader flagged absent.
* :func:`apps.shared._tagreader.require` raises :class:`ImportError` with a
  reinstall hint.
* Best-effort read paths (metadata scan, artwork probe, fingerprint bitrate,
  matcher title/artist probe, reconcile index) degrade to ``None`` /
  ``ok=False`` without exploding.
* No read path reaches for mutagen: the reads work with mutagen made
  unimportable (the positive control below).

Absence is real: the degrade test runs the production imports in a
subprocess where ``import tinytag`` fails, so the import guard itself sets
``HAS_TAG_READER``. The HTTP-level 503s are proven the same way in their own
tests.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup" / "src-128.mp3"

_DEPENDENTS = (
    "apps.shared.audio_files",
    "apps.shared.audio_playable",
    "apps.reconcile.index_disk",
)


def test_read_paths_degrade_with_tinytag_unimportable(tmp_path):
    """Every read path degrades cleanly in a process where tinytag is missing.

    Runs the production imports in a subprocess with ``import tinytag``
    blocked, so ``HAS_TAG_READER`` is computed by the real import guard, not
    patched. Positive control first: the probe proves tinytag really is
    unimportable and the gate saw it, else a degraded result could be a
    reader that simply failed on this file.
    """
    track = tmp_path / "x.mp3"
    shutil.copyfile(FIXTURE, track)
    probe = textwrap.dedent(
        f"""
        import importlib
        import sys
        sys.modules["tinytag"] = None
        try:
            import tinytag  # noqa: F401
        except ImportError:
            pass
        else:
            raise SystemExit("control failed: tinytag still importable")
        from pathlib import Path
        from apps.shared import _tagreader
        assert _tagreader.HAS_TAG_READER is False
        try:
            _tagreader.require()
        except ImportError as exc:
            assert "tinytag" in str(exc) and "uv sync" in str(exc), exc
        else:
            raise SystemExit("require() did not raise")
        for name in {(*_DEPENDENTS, "apps.shared.fingerprints", "apps.sync.matcher")!r}:
            importlib.import_module(name)
        from apps.reconcile import index_disk
        from apps.shared import audio_files, fingerprints
        from apps.sync import matcher
        p = Path({str(track)!r})
        assert _tagreader.can_read(p) is False
        assert audio_files.read_metadata(p) is None
        assert audio_files.read_embedded_artwork(p) is None
        assert audio_files.embedded_artwork_available(p) is False
        assert fingerprints._safe_bitrate(p) is None
        assert matcher._read_id3(p) is None
        assert index_disk.read_tags(p).ok is False
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith("OK")


def test_read_paths_measure_with_reader(tmp_path):
    """Positive control for the test above: the same file DOES read."""
    from apps.reconcile import index_disk
    from apps.shared import audio_files, fingerprints

    track = tmp_path / "x.mp3"
    shutil.copyfile(FIXTURE, track)
    meta = audio_files.read_metadata(track)
    assert meta is not None and meta.duration_s == pytest.approx(3.056, abs=0.01)
    assert fingerprints._safe_bitrate(track) == 127
    read = index_disk.read_tags(track)
    assert read.ok is True and read.duration_s == pytest.approx(3.056, abs=0.01)


def test_read_paths_never_import_mutagen(tmp_path):
    """Every read works in a process where ``import mutagen`` fails.

    Positive control first: the probe proves mutagen really is blocked, else
    a passing read could be mutagen's.
    """
    track = tmp_path / "x.mp3"
    shutil.copyfile(FIXTURE, track)
    # The matcher ignores untagged files, so give its read a tagged copy.
    # Written here, in the parent, because the probe process blocks mutagen.
    tagged = tmp_path / "tagged.mp3"
    shutil.copyfile(FIXTURE, tagged)
    mutagen_id3 = pytest.importorskip("mutagen.id3")
    tags = mutagen_id3.ID3()
    tags.add(mutagen_id3.TIT2(encoding=3, text="Probe Title"))
    tags.add(mutagen_id3.TPE1(encoding=3, text="Probe Artist"))
    tags.save(tagged)
    probe = textwrap.dedent(
        f"""
        import sys
        sys.modules["mutagen"] = None
        try:
            import mutagen  # noqa: F401
        except ImportError:
            pass
        else:
            raise SystemExit("control failed: mutagen still importable")
        from pathlib import Path
        from apps.reconcile import index_disk
        from apps.shared import audio_files, audio_playable, fingerprints
        from apps.sync import matcher
        p = Path({str(track)!r})
        meta = audio_files.read_metadata(p)
        assert meta is not None and meta.duration_s > 3, meta
        assert fingerprints._safe_bitrate(p) == 127
        assert index_disk.read_tags(p).ok
        assert matcher._read_id3(Path({str(tagged)!r})) is not None
        audio_playable.probe_playable_audio(p)
        assert "mutagen" not in {{k for k, v in sys.modules.items() if v is not None}}
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith("OK")


def test_playable_probe_accepts_raw_aac_tinytag_cannot_read(tmp_path):
    """A raw ADTS .aac passes ingest: tinytag has no reader for it.

    Regression from the tinytag switch (review of #4997): tinytag returns no
    duration for formats it cannot parse instead of raising, so the
    duration cross-check rejected every raw .aac as "missing or zero
    duration". The control below proves the cross-check still runs for a
    format tinytag does read.
    """
    from apps.shared import _tagreader, audio_playable

    adts_frame = bytes.fromhex("fff1508001 3ffc".replace(" ", "")) + b"\x00" * 1017
    track = tmp_path / "raw.aac"
    track.write_bytes(adts_frame * 64)
    assert _tagreader.can_read(track) is False
    audio_playable.probe_playable_audio(track)

    # Control: a tinytag-readable type with valid magic but no audio frames is
    # still rejected by the tag cross-check.
    bogus = tmp_path / "bogus.mp3"
    bogus.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 4096)
    assert _tagreader.can_read(bogus) is True
    with pytest.raises(audio_playable.UnplayableAudioError):
        audio_playable.probe_playable_audio(bogus)


@pytest.mark.parametrize("name", ["src-128.mp3", "src.flac", "src.m4a", "src.wav"])
def test_upload_hold_path_suffix_still_reads_duration(tmp_path, name):
    """The upload duplicate probe reads a ``<name>.part`` hold file.

    tinytag picks its parser from the extension first, then falls back to
    sniffing the content, so the ``.part`` suffix the upload route adds must
    not cost the duration it uses to find duplicates (review of #4997).
    """
    from apps.webui.server.routes import ingest_upload

    held = tmp_path / f"{name}.part"
    shutil.copyfile(FIXTURE.parent / name, held)
    duration = ingest_upload._duration_s(held)
    assert duration is not None and 2.9 < duration < 3.2, duration
