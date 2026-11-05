"""[if] a WAVEFORMATEXTENSIBLE wav GUID is PCM or IEEE-float [then] it imports, [else stop]."""

from __future__ import annotations

import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from apps.shared.audio_playable import UnplayableAudioError, probe_playable_audio
from tests.shared.audio_probe_fixtures import (
    GUID_IEEE_FLOAT,
    GUID_PCM,
    WAVE_FORMAT_ADPCM,
    WAVE_FORMAT_MP3,
    extensible_fmt,
    pcm_data,
    write_extensible_wav,
    write_float16_wav,
    write_pcm_wav,
    write_tagged_wav,
    write_wav,
    write_wav_missing_data,
    write_wav_missing_fmt,
    write_wav_post_data_truncated_header,
    write_wav_with_fact,
)

pytestmark = pytest.mark.requirement("SETUP-16")


def _write_stdlib_pcm(path: Path, seconds: float = 0.1) -> Path:
    frames = int(44100 * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return path


def _reject(path: Path, token: str) -> None:
    with pytest.raises(UnplayableAudioError) as caught:
        probe_playable_audio(path)
    assert token in caught.value.reason


def test_stdlib_pcm_wav_still_imports(tmp_path: Path) -> None:
    """[if] a stdlib PCM wav is probed [then] it imports, [else stop]."""
    path = _write_stdlib_pcm(tmp_path / "pcm.wav")
    probe_playable_audio(path)


def test_ieee_float_16_byte_fmt_imports(tmp_path: Path) -> None:
    """[if] a 16-byte IEEE-float fmt is probed [then] it imports, [else stop]."""
    path = write_float16_wav(tmp_path / "float.wav")
    probe_playable_audio(path)


def test_extensible_pcm_guid_imports(tmp_path: Path) -> None:
    """[if] extensible PCM GUID wav is probed [then] it imports, [else stop]."""
    path = write_extensible_wav(tmp_path / "ext-pcm.wav", guid=GUID_PCM, bits=16)
    probe_playable_audio(path)


def test_extensible_float_guid_imports(tmp_path: Path) -> None:
    """[if] extensible IEEE-float GUID wav is probed [then] it imports, [else stop]."""
    path = write_extensible_wav(tmp_path / "ext-float.wav", guid=GUID_IEEE_FLOAT, bits=32)
    probe_playable_audio(path)


def test_extensible_24_in_32_container_imports(tmp_path: Path) -> None:
    """[if] valid bits 24 sit in a 32-bit container [then] it imports, [else stop]."""
    path = write_extensible_wav(tmp_path / "ext-24.wav", bits=32, valid_bits=24)
    probe_playable_audio(path)


def test_extensible_matching_mask_imports(tmp_path: Path) -> None:
    """[if] channel mask popcount matches nChannels [then] it imports, [else stop]."""
    path = write_extensible_wav(tmp_path / "ext-mask.wav", mask=0x4)
    probe_playable_audio(path)


def test_pcm_with_valid_fact_imports(tmp_path: Path) -> None:
    """[if] a fact payload names a nonzero length [then] it imports, [else stop]."""
    path = write_wav_with_fact(tmp_path / "fact-ok.wav", fact_payload=struct.pack("<I", 4410))
    probe_playable_audio(path)


def test_wrong_guid_suffix_rejects(tmp_path: Path) -> None:
    """[if] extensible GUID suffix is wrong [then] probe rejects, [else stop]."""
    guid = GUID_PCM[:-1] + b"\x00"
    _reject(write_extensible_wav(tmp_path / "bad-guid.wav", guid=guid), "extensible subformat")


def test_guid_pcm_word_with_zero_suffix_rejects(tmp_path: Path) -> None:
    """[if] GUID starts as PCM but the rest is zeros [then] reject, [else stop]."""
    guid = GUID_PCM[:4] + b"\x00" * 12
    _reject(write_extensible_wav(tmp_path / "short-guid.wav", guid=guid), "extensible subformat")


def test_extensible_cbsize_21_rejects(tmp_path: Path) -> None:
    """[if] extensible cbSize is 21 [then] probe rejects, [else stop]."""
    _reject(write_extensible_wav(tmp_path / "cb21.wav", cb_size=21), "extensible")


def test_extensible_fmt_size_38_rejects(tmp_path: Path) -> None:
    """[if] extensible fmt payload is 38 bytes [then] probe rejects, [else stop]."""
    path = write_wav(tmp_path / "fmt38.wav", [(b"fmt ", extensible_fmt()[:38]), (b"data", pcm_data())])
    _reject(path, "truncated")


def test_valid_bits_exceed_container_rejects(tmp_path: Path) -> None:
    """[if] valid bits exceed container bits [then] probe rejects, [else stop]."""
    _reject(write_extensible_wav(tmp_path / "valid-bits.wav", bits=16, valid_bits=24), "extensible")


def test_mask_popcount_mismatch_rejects(tmp_path: Path) -> None:
    """[if] channel mask popcount mismatches nChannels [then] reject, [else stop]."""
    _reject(write_extensible_wav(tmp_path / "mask.wav", mask=0x3), "extensible")


def test_adpcm_tag_rejects_by_name(tmp_path: Path) -> None:
    """[if] wav tag is ADPCM 0x0002 [then] reject naming the tag, [else stop]."""
    _reject(write_tagged_wav(tmp_path / "adpcm.wav", WAVE_FORMAT_ADPCM), "unsupported wav format")


def test_mp3_in_wav_tag_rejects_by_name(tmp_path: Path) -> None:
    """[if] wav tag is MP3 0x0055 [then] reject naming the tag, [else stop]."""
    _reject(write_tagged_wav(tmp_path / "mp3.wav", WAVE_FORMAT_MP3), "unsupported wav format")


def test_missing_data_chunk_rejects(tmp_path: Path) -> None:
    """[if] a wav has fmt and no data [then] probe rejects, [else stop]."""
    _reject(write_wav_missing_data(tmp_path / "no-data.wav"), "data")


def test_missing_fmt_chunk_rejects(tmp_path: Path) -> None:
    """[if] a wav has data and no fmt [then] probe rejects, [else stop]."""
    _reject(write_wav_missing_fmt(tmp_path / "no-fmt.wav"), "fmt")


def test_truncated_fact_rejects(tmp_path: Path) -> None:
    """[if] a fact payload is shorter than 4 bytes [then] reject, [else stop]."""
    _reject(write_wav_with_fact(tmp_path / "fact-short.wav", fact_payload=b"\x01\x00"), "fact")


def test_zero_fact_length_rejects(tmp_path: Path) -> None:
    """[if] fact dwSampleLength is zero [then] probe rejects, [else stop]."""
    _reject(write_wav_with_fact(tmp_path / "fact-zero.wav", fact_payload=struct.pack("<I", 0)), "fact")


def test_post_data_truncated_header_rejects(tmp_path: Path) -> None:
    """[if] a chunk header after data crosses the container end [then] reject, [else stop]."""
    _reject(write_wav_post_data_truncated_header(tmp_path / "trail.wav"), "truncated")


@pytest.mark.requires_ffmpeg
def test_ffmpeg_six_channel_extensible_if_encoder_emits_it(tmp_path: Path) -> None:
    """[if] ffmpeg writes 6ch PCM as 0xFFFE [then] the probe imports it, [else stop]."""
    out = tmp_path / "six.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=0.1:sample_rate=22050",
            "-ac",
            "6",
            "-ar",
            "22050",
            "-c:a",
            "pcm_s16le",
            str(out),
        ],
        check=True,
    )
    raw = out.read_bytes()
    fmt_at = raw.find(b"fmt ")
    assert fmt_at >= 0
    tag = struct.unpack_from("<H", raw, fmt_at + 8)[0]
    if tag != 0xFFFE:
        pytest.fail(f"UNAVAILABLE: ffmpeg wrote tag 0x{tag:04x}, not 0xFFFE")
    probe_playable_audio(out)


def test_copy_of_pcm_control_still_imports(tmp_path: Path) -> None:
    """[if] a copied PCM control is probed [then] it still imports, [else stop]."""
    src = write_pcm_wav(tmp_path / "src.wav")
    dest = tmp_path / "copy.wav"
    shutil.copyfile(src, dest)
    probe_playable_audio(dest)
