"""Guard: scripts/stem_bundle_worker.py's stem output codec follows the
source-extension policy (apps/stems/stem_size_policy.py) instead of the old
hardcoded WAV output (issue #1497).

_stem_file_suffix and _manifest_dict are pure (no filesystem access, no
torch/demucs) so this guard runs on any box, with no GPU and no real audio
fixture required -- the full separation path (write_bundle) needs a
GPU/torch/demucs stack this suite does not have. The manifest round-trip
tests below DO touch the filesystem (real media + manifest.json under
tmp_path) to exercise the real production reader
(apps.webui.server.stem_artifacts.load_stem_bundle) against the schema v3
manifest shape this worker writes, since that reader is the actual contract
this fix must satisfy and _load_v3_bundle never reads file headers (audio
alignment comes from the manifest's own audio block, not the files).

Codex review (PR #1663) flagged an earlier draft that wrote fabricated
``b"not-real-audio"`` bytes for the stem parts: a reader that only trusts the
filename suffix and manifest metadata stays green even over corrupt or
undecodable media, so it gave no acceptance evidence for the codec path
itself. This version instead builds genuinely decodable media: a stdlib
``wave`` PCM16 WAV, piped through the real ``ffmpeg`` binary to real FLAC
bytes, and -- for the mp3 case -- through the actual production
``worker._ffmpeg_encode_mp3`` function to real mp3 bytes. ``ffprobe``
decodability assertions on the written files are the acceptance evidence.
Honest gap: the production ``_encode_flac_bytes`` function itself (the
tensor -> FLAC path) is NOT exercised here, because it needs numpy/soundfile,
which are deliberately excluded from this repo's venv (heavy ML deps stay in
PEP 723 scripts, see .claude/rules/python-backend.md). The FLAC bytes here
come from the same ffmpeg binary via a direct subprocess call instead, which
is the closest same-binary substitute available in this environment.

  - [if] the source extension is .mp3 [then] the stem suffix is .mp3, [else stop]
  - [if] a v3 bundle's mp3 parts are real ffmpeg-encoded media [then] ffprobe
    reports a positive duration for each part, [else stop]
  - [if] a v3 bundle's flac parts are real ffmpeg-encoded media [then] ffprobe
    reports a positive duration for each part, [else stop]
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import wave
from pathlib import Path

import pytest

import scripts.stem_bundle_worker as worker
from apps.stems.stem_size_policy import UnknownSourceFormatError
from apps.webui.server.stem_artifacts import load_stem_bundle

pytestmark = pytest.mark.requirement("STEM-02")

FFMPEG = os.environ.get("MDT_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("MDT_FFPROBE", "ffprobe")


def test_mp3_source_gets_mp3_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.mp3")) == ".mp3"


def test_m4a_source_gets_mp3_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.m4a")) == ".mp3"


def test_flac_source_gets_flac_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.flac")) == ".flac"


def test_wav_source_gets_flac_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.wav")) == ".flac"


def test_unrecognised_source_extension_raises() -> None:
    with pytest.raises(UnknownSourceFormatError):
        worker._stem_file_suffix(Path("track.ogg"))


def _silence_wav_bytes(*, sample_rate: int = 44100, channels: int = 2, frames: int = 4410) -> bytes:
    """A tiny, genuinely valid PCM16 WAV (stdlib `wave`, no numpy) used as
    encoder input for the codec round-trip tests below."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * channels * frames)
    return buffer.getvalue()


def _wav_to_real_flac_bytes(wav_bytes: bytes, out_path: Path) -> bytes:
    """Real FLAC bytes via the same ffmpeg binary the production mp3 encoder
    shells out to (not a call into _encode_flac_bytes: that function needs
    numpy/soundfile, deliberately excluded from this repo's venv)."""
    proc = subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-codec:a",
            "flac",
            str(out_path),
        ],
        input=wav_bytes,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0 and out_path.is_file(), proc.stderr.decode(errors="replace")
    return out_path.read_bytes()


def _ffprobe_duration_s(path: Path) -> float:
    """ffprobe's own reported duration: acceptance evidence that ``path`` is
    genuinely decodable media, not just a suffix a reader trusted blindly."""
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return float(proc.stdout.strip())


def _write_stub_bundle(
    tmp_path: Path, stable_id: str, *, ext: str, codec: str, part_bytes: bytes
) -> Path:
    """Write a schema v3 bundle (real, decodable part bytes shared across all
    4 parts, real manifest.json) so the production reader AND ffprobe can be
    exercised without a GPU or a real separated track."""
    bundle_dir = tmp_path / stable_id
    bundle_dir.mkdir()
    for name in worker.STEM_PARTS:
        (bundle_dir / f"{name}{ext}").write_bytes(part_bytes)
    manifest = worker._manifest_dict(
        stable_id=stable_id,
        audio_path=Path(f"/tmp/source{'.mp3' if codec == 'mp3' else '.flac'}"),
        source_sha256="0" * 64,
        ext=ext,
        output_codec=codec,
        audio={"sample_rate": 44100, "frame_count": 1000, "channels": 2},
        size_policy={
            "source_bytes": 12345,
            "source_kbps": 256.0,
            "mp3_settings": None,
            "part_bytes": {name: 15 for name in worker.STEM_PARTS},
            "violations": [],
        },
    )
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


@pytest.mark.requires_ffmpeg
def test_mp3_schema_v3_manifest_round_trips_through_real_reader(tmp_path: Path) -> None:
    flac_bytes = _wav_to_real_flac_bytes(_silence_wav_bytes(), tmp_path / "probe.flac")
    mp3_bytes = worker._ffmpeg_encode_mp3(flac_bytes, tmp_path / "probe.mp3", cbr_kbps=None)

    root = _write_stub_bundle(tmp_path, "teststem-mp3", ext=".mp3", codec="mp3", part_bytes=mp3_bytes)
    bundle = load_stem_bundle("teststem-mp3", roots=[root])
    assert bundle.media_type == "audio/mpeg"
    assert bundle.layout == "demucs4"
    for name in worker.STEM_PARTS:
        assert _ffprobe_duration_s(root / "teststem-mp3" / f"{name}.mp3") > 0


@pytest.mark.requires_ffmpeg
def test_flac_schema_v3_manifest_round_trips_through_real_reader(tmp_path: Path) -> None:
    flac_bytes = _wav_to_real_flac_bytes(_silence_wav_bytes(), tmp_path / "probe.flac")

    root = _write_stub_bundle(tmp_path, "teststem-flac", ext=".flac", codec="flac", part_bytes=flac_bytes)
    bundle = load_stem_bundle("teststem-flac", roots=[root])
    assert bundle.media_type == "audio/flac"
    assert bundle.layout == "demucs4"
    for name in worker.STEM_PARTS:
        assert _ffprobe_duration_s(root / "teststem-flac" / f"{name}.flac") > 0
