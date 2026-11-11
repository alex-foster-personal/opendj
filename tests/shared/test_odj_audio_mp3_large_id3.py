"""Large leading ID3v2 tags: odj-audio probe and decode (issue #11372, parent #4863)."""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from apps.shared import platform_paths
from tests.rust_build_env import build_audio_engine

CLICK_MP3 = platform_paths.PROJECT_ROOT / "apps/audio-engine/tests/fixtures/audio/click-250ms.mp3"


def _id3_syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _mp3_with_priv_tag(mp3: bytes, frame_body_bytes: int) -> bytes:
    payload = b"\x00" * frame_body_bytes
    frame = b"PRIV" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload
    return b"ID3\x03\x00\x00" + _id3_syncsafe(len(frame)) + frame + mp3


def _snr_db(got: list[float], want: list[float]) -> float:
    n = min(len(got), len(want))
    err = sum((got[i] - want[i]) ** 2 for i in range(n))
    sig = sum(want[i] ** 2 for i in range(n))
    if sig == 0.0:
        return float("-inf") if err else float("inf")
    return 10.0 * math.log10(err / sig)


@pytest.fixture(scope="module")
def odj_audio() -> Path:
    return build_audio_engine(platform_paths.PROJECT_ROOT / "apps" / "audio-engine")


def _run(binary: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[bytes | str]:
    env = {**os.environ, "RUSTFLAGS": os.environ.get("RUSTFLAGS", "-C linker=gcc")}
    return subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=text,
        check=False,
        timeout=120,
        env=env,
    )


def _decode_mono_f32(binary: Path, path: Path) -> list[float]:
    proc = _run(binary, "decode", "--mono", str(path), text=False)
    assert proc.returncode == 0, proc.stderr.decode() if proc.stderr else ""
    raw = proc.stdout
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def _ffmpeg_mono_f32(path: Path) -> list[float] | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le", "-"],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        return None
    stereo = struct.unpack(f"<{len(proc.stdout) // 4}f", proc.stdout)
    return list(stereo[0::2])


@pytest.fixture
def tagged_4mb(tmp_path: Path) -> Path:
    path = tmp_path / "tagged-4mb.mp3"
    path.write_bytes(_mp3_with_priv_tag(CLICK_MP3.read_bytes(), 4_000_000))
    return path


@pytest.mark.requirement("AUDIO-12688")
def test_a_4_mb_id3v2_mp3_decodes_via_odj_audio(odj_audio: Path, tagged_4mb: Path) -> None:
    """[if] an MP3 with a 4 MB ID3v2 tag is opened [then] odj-audio decodes it, [else stop]."""
    proc = _run(odj_audio, "probe", str(tagged_4mb), text=True)
    assert proc.returncode == 0, proc.stderr
    combined = (proc.stderr or "") + (proc.stdout or "")
    assert "no suitable format reader found" not in combined
    assert json.loads(proc.stdout or "{}")["sample_rate"] == 44100
    pcm = _decode_mono_f32(odj_audio, tagged_4mb)
    assert max(abs(s) for s in pcm) > 0.01


@pytest.mark.requirement("AUDIO-12688")
def test_a_4_mb_tagged_mp3_pcm_matches_ffmpeg_at_least_120_db(odj_audio: Path, tagged_4mb: Path) -> None:
    """[if] that tagged MP3 is compared to ffmpeg after alignment [then] PCM SNR >= 120 dB, [else stop]."""
    want = _ffmpeg_mono_f32(tagged_4mb)
    if want is None:
        pytest.skip("UNAVAILABLE: ffmpeg not on PATH")
    got = _decode_mono_f32(odj_audio, tagged_4mb)
    n = min(len(got), len(want))
    snr = _snr_db(got[:n], want[:n])
    assert snr <= -120.0, f"SNR {snr:.1f} dB"


@pytest.mark.requirement("AUDIO-12688")
def test_an_mp3_with_a_990_kb_id3v2_tag_still_opens(odj_audio: Path, tmp_path: Path) -> None:
    """[if] an MP3 tag is 0.99 MB or less [then] it still opens, [else stop]."""
    path = tmp_path / "tagged-990k.mp3"
    path.write_bytes(_mp3_with_priv_tag(CLICK_MP3.read_bytes(), 990 * 1024))
    proc = _run(odj_audio, "probe", str(path), text=True)
    assert proc.returncode == 0, proc.stderr
    _decode_mono_f32(odj_audio, path)


@pytest.mark.requirement("AUDIO-12688")
def test_a_non_audio_file_without_id3_fails_probe_quickly(odj_audio: Path, tmp_path: Path) -> None:
    """[if] a non-audio file with no ID3 is probed [then] named error not hang, [else stop]."""
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\xde" * (64 * 1024))
    proc = _run(odj_audio, "probe", str(junk), text=True)
    assert proc.returncode != 0
    msg = (proc.stderr or "") + (proc.stdout or "")
    assert "unrecognized format" in msg or "no audio track" in msg


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
