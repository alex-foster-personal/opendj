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
    # The matcher ignores untagged files, so read an already-tagged fixture
    # (title "Source V2", artist "Fixture"); no tagging step can skip this.
    tagged = FIXTURE.parent / "src-v2.mp3"
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
        assert matcher._read_id3(Path({str(tagged)!r})) == ("Source V2", "Fixture")
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

    track = tmp_path / "raw.aac"
    track.write_bytes(_adts_frames(64))
    assert _tagreader.can_read(track) is False
    audio_playable.probe_playable_audio(track)

    # A bare ADTS sync word passes the magic check but holds no frame.
    stub = tmp_path / "stub.aac"
    stub.write_bytes(b"\xff\xf1")
    with pytest.raises(audio_playable.UnplayableAudioError, match="adts frames"):
        audio_playable.probe_playable_audio(stub)

    # A header promising a whole frame the file does not hold is truncated.
    cut = tmp_path / "cut.aac"
    cut.write_bytes(_adts_frames(1)[:27])
    assert _tagreader.adts_duration(cut) is None

    # A whole frame followed by a truncated one is truncated audio too...
    tail = tmp_path / "tail.aac"
    tail.write_bytes(_adts_frames(2)[:1024 + 27])
    assert _tagreader.adts_duration(tail) is None
    # So is a stream cut a few bytes into the next header.
    for keep in (1, 2, 6):
        short = tmp_path / f"short{keep}.aac"
        short.write_bytes(_adts_frames(2)[: 1024 + keep])
        assert _tagreader.adts_duration(short) is None, keep
    # And a full header with ADTS sync but a reserved sample-rate index.
    bad = tmp_path / "bad.aac"
    bad.write_bytes(_adts_frames(1) + _adts_frames(1, rate_index=15))
    assert _tagreader.adts_duration(bad) is None
    # A later frame at another sample rate is not the same stream either.
    mixed = tmp_path / "mixed.aac"
    mixed.write_bytes(_adts_frames(1, rate_index=4) + _adts_frames(1, rate_index=3))
    assert _tagreader.adts_duration(mixed) is None
    # ...while a trailing ID3v1 tag (no ADTS sync) is not a frame at all.
    tagged_tail = tmp_path / "tagged_tail.aac"
    tagged_tail.write_bytes(_adts_frames(2) + b"TAG" + b"\x00" * 125)
    assert _tagreader.adts_duration(tagged_tail) == pytest.approx(2 * 1024 / 44100)
    with pytest.raises(audio_playable.UnplayableAudioError, match="adts frames"):
        audio_playable.probe_playable_audio(cut)

    # .alac (MP4) is off tinytag's extension list but parses by content, so
    # it gets the tag cross-check too: real audio passes, a bare ftyp fails.
    alac = tmp_path / "real.alac"
    shutil.copyfile(FIXTURE.parent / "src.m4a", alac)
    audio_playable.probe_playable_audio(alac)
    stub_alac = tmp_path / "stub.alac"
    stub_alac.write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 4096)
    with pytest.raises(audio_playable.UnplayableAudioError):
        audio_playable.probe_playable_audio(stub_alac)

    # An MP4 container named .aac goes to the tag reader, not the ADTS walk.
    mp4 = tmp_path / "mp4.aac"
    shutil.copyfile(FIXTURE.parent / "src.m4a", mp4)
    audio_playable.probe_playable_audio(mp4)

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


def test_fingerprint_cache_backfills_a_null_bitrate(tmp_path):
    """A cache row stored with no bitrate gets it on the next lookup.

    Rows cached while no tag reader was installed hold NULL, which canonical
    selection reads as 0 (review of #4997). Control: the row really is NULL
    before the lookup, and a second lookup reads the stored value.
    """
    import sqlite3

    from apps.shared.fingerprints import Fingerprint, FingerprintCache

    track = tmp_path / "x.mp3"
    shutil.copyfile(FIXTURE, track)
    st = track.stat()
    cache = FingerprintCache(tmp_path / "fp.sqlite")
    cache.put(
        Fingerprint(
            path=track, duration=3.0, fp_str="AQAA", size=st.st_size,
            mtime=st.st_mtime, bitrate=None,
        )
    )
    with sqlite3.connect(tmp_path / "fp.sqlite") as conn:
        assert conn.execute("SELECT bitrate FROM fingerprints").fetchone() == (None,)
    assert cache.get(track).bitrate == 127
    with sqlite3.connect(tmp_path / "fp.sqlite") as conn:
        assert conn.execute("SELECT bitrate FROM fingerprints").fetchone() == (127,)


def _adts_frames(count: int, rate_index: int = 4, frame_len: int = 1024) -> bytes:
    """``count`` ADTS frames (AAC-LC, mono, no CRC) of ``frame_len`` bytes."""
    header = bytes([
        0xFF, 0xF1,
        (1 << 6) | (rate_index << 2),
        (1 << 6) | ((frame_len >> 11) & 0x03),
        (frame_len >> 3) & 0xFF,
        ((frame_len & 0x07) << 5) | 0x1F,
        0xFC,
    ])
    return (header + b"\x00" * (frame_len - 7)) * count


def test_upload_duration_reads_raw_aac_held_as_part(tmp_path):
    """A raw ADTS upload gets a true duration, so the duplicate check runs.

    tinytag has no ADTS reader and reports a wrong, tiny duration (0.03 s
    for a 7.3 s stream), and a None duration skipped duplicate detection
    entirely (review of #4997). Uploads are held as ``.part``, so the walk
    must work by content. Control: other formats still read via tinytag.
    """
    from apps.shared import _tagreader
    from apps.webui.server.routes import ingest_upload

    hold = tmp_path / "raw.aac.part"
    hold.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x05" + b"\x00" * 5 + _adts_frames(430))
    assert ingest_upload._duration_s(hold) == pytest.approx(430 * 1024 / 44100)

    assert _tagreader.adts_duration(FIXTURE) is None
    assert ingest_upload._duration_s(FIXTURE) == pytest.approx(3.06, abs=0.05)


def test_shared_read_reports_true_raw_aac_duration(tmp_path):
    """Every duration consumer, not only uploads, gets the ADTS frame walk.

    Folder ingest and the disk index call ``_tagreader.read`` directly, so a
    tinytag misread would persist there (review of #4997). Control: an
    ``mp3`` keeps tinytag's own duration.
    """
    from apps.shared import _tagreader

    track = tmp_path / "raw.aac"
    track.write_bytes(_adts_frames(430))
    tag = _tagreader.read(track)
    assert tag.duration == pytest.approx(430 * 1024 / 44100)
    # tinytag fills the other stream properties from the same misread frames,
    # so they come from the walk too: 1024-byte frames over 1024 samples.
    assert (tag.samplerate, tag.channels) == (44100, 1)
    assert tag.bitrate == pytest.approx(1024 * 8 * 44100 / 1024 / 1000)
    assert _tagreader.read(FIXTURE).duration == pytest.approx(3.06, abs=0.05)
