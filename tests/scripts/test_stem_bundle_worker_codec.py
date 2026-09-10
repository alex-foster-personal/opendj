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
itself. A second draft built real media by piping a stdlib ``wave`` PCM16 WAV
through the ``ffmpeg`` binary directly for FLAC, bypassing the production
``worker._encode_flac_bytes`` (tensor -> FLAC) function entirely -- so the
test stayed green even if that function itself emitted corrupt media, and its
stub manifest's ``frame_count`` (1000) did not match the audio it declared
that count for (the WAV helper's own default was 4410 frames), so no test
here was actually checking alignment between what the manifest claims and
what the encoder produced.

This version calls the REAL production ``worker._encode_flac_bytes`` for both
codec round trips (mp3 goes through it too, exactly as ``_encode_stem_part``
chains ``_encode_flac_bytes`` -> ``_ffmpeg_encode_mp3`` in production). That
function calls exactly three methods on its ``tensor: Any`` argument --
``.detach().cpu().numpy()`` -- so ``_CpuTensor`` below wraps a genuine numpy
array to satisfy that documented subset without pulling torch into this
repo's venv (torch stays out per .claude/rules/python-backend.md; only the
torch OBJECT is stood in for, the array data and the encode call are real).
soundfile is a real dependency this needs (the ``analysis`` extra,
``pyproject.toml`` line 99), so these tests are ``requires_audio_stack``-gated:
UNAVAILABLE with a stated reason on a bare ``uv sync`` dev venv, and real,
measured evidence in CI, which installs ``requirements.txt`` (soundfile is
pinned there, line 159) -- the same honest-skip convention
``tests/conftest.py`` already uses for the analysis backend's own end-to-end
test. ``FRAMES``/``SAMPLE_RATE``/``CHANNELS`` below are the single source of
truth for both the tensor fed to the real encoder and the manifest's declared
``audio`` block, so the two cannot drift the way the 1000-vs-4410 mismatch
did. ``ffprobe``'s own decoded duration for each written part, checked
against ``FRAMES / SAMPLE_RATE``, is the alignment acceptance evidence:
measured against this exact production path (real ffmpeg VBR mp3 via a
seekable file, real FLAC via soundfile), the decoded duration matches the
declared one exactly (see commit body), so a tight tolerance still catches a
genuinely corrupted or misaligned encode without being loose acceptance
theater.

  - [if] the source extension is .mp3 [then] the stem suffix is .mp3, [else stop]
  - [if] a v3 bundle's mp3 parts are encoded by the real production
    ``_encode_flac_bytes`` -> ``_ffmpeg_encode_mp3`` chain [then] each part's
    ffprobe duration matches ``FRAMES / SAMPLE_RATE`` within 10ms, [else stop]
  - [if] a v3 bundle's flac parts are encoded by the real production
    ``_encode_flac_bytes`` [then] each part's ffprobe duration matches
    ``FRAMES / SAMPLE_RATE`` within 10ms, [else stop]
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import scripts.stem_bundle_worker as worker
from apps.stems.stem_size_policy import UnknownSourceFormatError
from apps.webui.server.stem_artifacts import load_stem_bundle

pytestmark = pytest.mark.requirement("STEM-02")

FFMPEG = os.environ.get("MDT_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("MDT_FFPROBE", "ffprobe")

# Single source of truth for the codec round-trip tests: fed into the real
# encoder AND the manifest's declared audio block, so they cannot disagree.
SAMPLE_RATE = 44100
CHANNELS = 2
FRAMES = 4410
DURATION_TOLERANCE_S = 0.01


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


class _CpuTensor:
    """Stands in for a torch.Tensor across exactly the three calls
    ``_encode_flac_bytes`` makes on its ``tensor: Any`` argument --
    ``.detach().cpu().numpy()`` -- so that real production function runs for
    real against a genuine numpy array, without pulling torch into this
    repo's venv."""

    def __init__(self, array: np.ndarray) -> None:
        self._array = array

    def detach(self) -> _CpuTensor:
        return self

    def cpu(self) -> _CpuTensor:
        return self

    def numpy(self) -> np.ndarray:
        return self._array


def _silence_tensor(*, channels: int = CHANNELS, frames: int = FRAMES) -> Any:
    """A genuinely all-zero (silent, decodable) (channels, frames) float32
    array -- the shape ``_encode_flac_bytes``' own ``.T`` expects -- wrapped
    as a real encoder input."""
    return _CpuTensor(np.zeros((channels, frames), dtype=np.float32))


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
    exercised without a GPU or a real separated track.

    ``audio`` is built from the SAME ``SAMPLE_RATE``/``FRAMES``/``CHANNELS``
    constants the caller fed to the real encoder, not independent literals --
    the earlier draft's declared ``frame_count`` (1000) never matched the
    4410 frames it actually encoded, so this manifest could not have caught a
    real misalignment."""
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
        audio={"sample_rate": SAMPLE_RATE, "frame_count": FRAMES, "channels": CHANNELS},
        size_policy={
            "source_bytes": 12345,
            "source_kbps": 256.0,
            "mp3_settings": None,
            "part_bytes": {name: len(part_bytes) for name in worker.STEM_PARTS},
            "violations": [],
        },
    )
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def _assert_parts_decode_to_declared_duration(root: Path, stable_id: str, ext: str) -> None:
    """Alignment evidence: what each part file ACTUALLY decodes to (ffprobe,
    reading the real media header/frames) must match what the manifest
    DECLARES (FRAMES / SAMPLE_RATE) -- the cross-check the reader itself
    intentionally skips (it trusts the manifest's own audio block, see module
    docstring), so a fixture that does not enforce it here proves nothing
    about the real encoder's alignment."""
    expected_duration_s = FRAMES / SAMPLE_RATE
    for name in worker.STEM_PARTS:
        actual_duration_s = _ffprobe_duration_s(root / stable_id / f"{name}{ext}")
        assert abs(actual_duration_s - expected_duration_s) < DURATION_TOLERANCE_S, (
            f"{name}{ext}: declared {expected_duration_s:.4f}s, ffprobe decoded "
            f"{actual_duration_s:.4f}s"
        )


@pytest.mark.requires_audio_stack
@pytest.mark.requires_ffmpeg
def test_mp3_schema_v3_manifest_round_trips_through_real_reader(tmp_path: Path) -> None:
    # Mirrors production _encode_stem_part's real chain: _encode_flac_bytes
    # (tensor -> FLAC, soundfile) then _ffmpeg_encode_mp3 (FLAC -> mp3, ffmpeg).
    flac_bytes = worker._encode_flac_bytes(_silence_tensor(), SAMPLE_RATE)
    mp3_bytes = worker._ffmpeg_encode_mp3(flac_bytes, tmp_path / "probe.mp3", cbr_kbps=None)

    root = _write_stub_bundle(
        tmp_path, "teststem-mp3", ext=".mp3", codec="mp3", part_bytes=mp3_bytes
    )
    bundle = load_stem_bundle("teststem-mp3", roots=[root])
    assert bundle.media_type == "audio/mpeg"
    assert bundle.layout == "demucs4"
    _assert_parts_decode_to_declared_duration(root, "teststem-mp3", ".mp3")


@pytest.mark.requires_audio_stack
@pytest.mark.requires_ffmpeg
def test_flac_schema_v3_manifest_round_trips_through_real_reader(tmp_path: Path) -> None:
    # Real production tensor -> FLAC path (soundfile), not a same-binary
    # ffmpeg substitute: this is the function PR #1663's review flagged as
    # unexercised.
    flac_bytes = worker._encode_flac_bytes(_silence_tensor(), SAMPLE_RATE)

    root = _write_stub_bundle(
        tmp_path, "teststem-flac", ext=".flac", codec="flac", part_bytes=flac_bytes
    )
    bundle = load_stem_bundle("teststem-flac", roots=[root])
    assert bundle.media_type == "audio/flac"
    assert bundle.layout == "demucs4"
    _assert_parts_decode_to_declared_duration(root, "teststem-flac", ".flac")
