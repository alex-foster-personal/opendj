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

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup" / "src-128.mp3"
# Real raw ADTS AAC: src.m4a's own AAC-LC stream remuxed without re-encoding
# (``ffmpeg -i src.m4a -c copy -f adts src.aac``), 22050 Hz mono. ffprobe
# reports 3.050 s; the frame walk counts the encoder priming the m4a's edit
# list hides, so it reads 3.065 s.
REAL_AAC = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup" / "src.aac"
REAL_AAC_SHA256 = "2176a32dd99822d77c2c7038e1fa97a6a315851d6f9b2724c8f112c2e86481e4"
REAL_AAC_SECONDS = 3.065


def _real_aac() -> bytes:
    """The real ADTS fixture's bytes, refused if they are not the checked-in file."""
    import hashlib

    data = REAL_AAC.read_bytes()
    assert hashlib.sha256(data).hexdigest() == REAL_AAC_SHA256, "src.aac changed"
    return data

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
    track.write_bytes(_real_aac())
    assert _tagreader.can_read(track) is False
    assert _tagreader.adts_duration(track) == pytest.approx(REAL_AAC_SECONDS, abs=0.01)
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
    # And a later frame whose sync byte is corrupted is damage, not a tag.
    flipped = bytearray(_adts_frames(2))
    flipped[1024] = 0xFE
    corrupt = tmp_path / "corrupt.aac"
    corrupt.write_bytes(bytes(flipped))
    assert _tagreader.adts_duration(corrupt) is None
    # A later frame at another sample rate is not the same stream either.
    mixed = tmp_path / "mixed.aac"
    mixed.write_bytes(_adts_frames(1, rate_index=4) + _adts_frames(1, rate_index=3))
    assert _tagreader.adts_duration(mixed) is None
    # ...while a trailing ID3v1 tag (no ADTS sync) is not a frame at all.
    tagged_tail = tmp_path / "tagged_tail.aac"
    tagged_tail.write_bytes(_real_aac() + b"TAG" + b"\x00" * 125)
    assert _tagreader.adts_duration(tagged_tail) == pytest.approx(REAL_AAC_SECONDS, abs=0.01)
    audio_playable.probe_playable_audio(tagged_tail)
    for sig in (b"APETAGEX", b"LYRICSBEGIN"):
        tail_tag = tmp_path / "tail_tag.aac"
        tail_tag.write_bytes(_real_aac() + sig + b"\x00" * 32)
        assert _tagreader.adts_duration(tail_tag) == pytest.approx(REAL_AAC_SECONDS, abs=0.01), sig
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
    hold.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x05" + b"\x00" * 5 + _real_aac())
    assert ingest_upload._duration_s(hold) == pytest.approx(REAL_AAC_SECONDS, abs=0.01)

    assert _tagreader.adts_duration(FIXTURE) is None
    assert ingest_upload._duration_s(FIXTURE) == pytest.approx(3.06, abs=0.05)


def test_upload_refuses_a_damaged_raw_aac(tmp_path):
    """A truncated ADTS upload 422s and leaves no hold file, not a ``new`` stage.

    tinytag misreads raw ADTS as MPEG with a bogus positive duration, so a
    failed frame walk must not fall back to it (review of #4997). Control:
    the same frames intact stage as new through the production duplicate
    lookup, against a disposable real state DB (``MDT_DATA_DIR`` is read at
    import, so the upload runs in its own interpreter).
    """
    frames = _real_aac()
    (tmp_path / "cut.bin").write_bytes(frames[:-100])
    (tmp_path / "ok.bin").write_bytes(frames)
    # A truncated non-ADTS file tinytag cannot parse is refused the same way.
    (tmp_path / "cutflac.bin").write_bytes((FIXTURE.parent / "src.flac").read_bytes()[:60])
    dest = tmp_path / "staged"
    dest.mkdir()
    code = textwrap.dedent(
        f"""
        import io, json
        from pathlib import Path
        from fastapi import HTTPException, UploadFile
        from apps.shared.state.db import open_rw
        from apps.shared.state import paths
        from apps.webui.server.routes import ingest_upload

        open_rw(paths.STATE_DB).close()
        src, dest = Path({str(tmp_path)!r}), Path({str(dest)!r})
        out = {{"db": str(paths.STATE_DB)}}
        try:
            ingest_upload._stage_one_upload(
                dest, UploadFile(io.BytesIO((src / "cut.bin").read_bytes()), filename="cut.aac"),
                "b", False,
            )
            out["cut"] = "staged"
        except HTTPException as exc:
            out["cut"] = exc.status_code
        out["left"] = sorted(p.name for p in dest.iterdir())
        try:
            ingest_upload._stage_one_upload(
                dest, UploadFile(io.BytesIO((src / "cutflac.bin").read_bytes()), filename="cut.flac"),
                "b", False,
            )
            out["cutflac"] = "staged"
        except HTTPException as exc:
            out["cutflac"] = exc.status_code
        out["left_flac"] = sorted(p.name for p in dest.iterdir())
        ok = ingest_upload._stage_one_upload(
            dest, UploadFile(io.BytesIO((src / "ok.bin").read_bytes()), filename="ok.aac"),
            "b", False,
        )
        out["ok"] = [ok.verdict, ok.duration_s, ok.fingerprint_method]
        print(json.dumps(out))
        """
    )
    env = {**os.environ, "MDT_DATA_DIR": str(tmp_path / "data")}
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["db"] == str(tmp_path / "data" / "state" / "state.db")
    assert out["cut"] == 422
    assert out["left"] == []
    assert out["cutflac"] == 422
    assert out["left_flac"] == []
    assert out["ok"][0] == "new"
    assert out["ok"][1] == pytest.approx(REAL_AAC_SECONDS, abs=0.01)
    assert out["ok"][2] == "duration"


def test_shared_read_reports_true_raw_aac_duration(tmp_path):
    """Every duration consumer, not only uploads, gets the ADTS frame walk.

    Folder ingest and the disk index call ``_tagreader.read`` directly, so a
    tinytag misread would persist there (review of #4997). Control: an
    ``mp3`` keeps tinytag's own duration.
    """
    from apps.shared import _tagreader

    track = tmp_path / "raw.aac"
    track.write_bytes(_real_aac())
    tag = _tagreader.read(track)
    assert tag.duration == pytest.approx(REAL_AAC_SECONDS, abs=0.01)
    # tinytag fills the other stream properties from the same misread frames,
    # so they come from the walk too; ffprobe reads this stream as 22050 Hz
    # mono, and its bitrate is the frame bytes over that duration.
    assert (tag.samplerate, tag.channels) == (22050, 1)
    assert tag.bitrate == pytest.approx(len(_real_aac()) * 8 / tag.duration / 1000)
    # tinytag reads the same AAC in its m4a container on its own: a
    # cross-check from an independent parser, within the priming offset.
    assert _tagreader.read(REAL_AAC.with_suffix(".m4a")).duration == pytest.approx(
        tag.duration, abs=0.1
    )
    assert _tagreader.read(FIXTURE).duration == pytest.approx(3.06, abs=0.05)


def test_shared_read_rejects_a_damaged_adts_stream(tmp_path):
    """A file that opens as ADTS but fails the walk raises, never tinytag's misread.

    tinytag reports a bogus positive duration for raw ADTS, so falling back
    to it would persist those values (review of #4997). Control: the
    same frames intact read cleanly.
    """
    from apps.shared import _tagreader

    track = tmp_path / "cut.aac"
    track.write_bytes(_real_aac()[:-100])
    with pytest.raises(_tagreader.TagReadError):
        _tagreader.read(track)
    assert _tagreader.read(track, duration=False) is not None
    # A sync word alone, or a sync-like first header that is invalid, is
    # still ADTS and still refused (review of #4997).
    for head in (b"\xff\xf1", b"\xff\xf1\xff\xff\xff\xff\xff" + b"\x00" * 64):
        track.write_bytes(head)
        assert _tagreader.starts_with_adts(track)
        with pytest.raises(_tagreader.TagReadError):
            _tagreader.read(track)
    # A leading ID3v2 tag declaring an extent past EOF hides the stream;
    # it is refused, not read as "not ADTS" (review of #4997).
    track.write_bytes(b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f" + _adts_frames(2))
    assert _tagreader.starts_with_adts(track)
    with pytest.raises(_tagreader.TagReadError):
        _tagreader.read(track)
    assert not _tagreader.starts_with_adts(FIXTURE)
    # A file gone between tinytag's read and the frame walk is a read error
    # for that file, not an OSError that aborts a whole library walk.
    gone = tmp_path / "gone.aac"
    with pytest.raises(_tagreader.TagReadError):
        _tagreader._apply_adts(gone, None)
    track.write_bytes(_real_aac())
    assert _tagreader.read(track).duration == pytest.approx(REAL_AAC_SECONDS, abs=0.01)
