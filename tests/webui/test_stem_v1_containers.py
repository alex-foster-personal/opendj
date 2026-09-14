"""v1 stem bundles must load MP3 and FLAC parts by their own container."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes.stems import router
from apps.webui.server.stem_artifacts import (
    FRAME_MISMATCH_TOL_S,
    StemArtifactError,
    WavMetadata,
    _find_mp3_sync,
    _flac_block_size_from_header,
    _id3v2_skip_size,
    load_stem_bundle,
    read_mp3_metadata,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
LAME = shutil.which("lame")


def _require_ffmpeg() -> str:
    if FFMPEG is None:
        pytest.skip("ffmpeg is required to synthesize real MP3/FLAC stem fixtures")
    return FFMPEG


def _write_wav(path: Path, *, frames: int = 12, sample_rate: int = 44_100) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames * 2)


def _write_sine_wav(path: Path, *, duration_s: float = 0.25, sample_rate: int = 44_100) -> None:
    ffmpeg = _require_ffmpeg()
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_s}:sample_rate={sample_rate}",
            "-ac",
            "2",
            str(path),
        ],
        check=True,
    )


def _write_silence_wav(path: Path, *, duration_s: float = 0.25, sample_rate: int = 44_100) -> None:
    ffmpeg = _require_ffmpeg()
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=stereo:d={duration_s}",
            str(path),
        ],
        check=True,
    )


def _require_ffprobe() -> str:
    if FFPROBE is None:
        pytest.skip("ffprobe is required to cross-check VBR MP3 sample counts")
    return FFPROBE


def _ffprobe_sample_count(path: Path) -> int:
    ffprobe = _require_ffprobe()
    output = subprocess.check_output(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration,sample_rate",
            "-of",
            "json",
            str(path),
        ],
        text=True,
    )
    payload = json.loads(output)
    stream = payload["streams"][0]
    duration = float(stream["duration"])
    sample_rate = int(stream["sample_rate"])
    return round(duration * sample_rate)


def _encode_joint_stereo_mp3(source: Path, dest: Path) -> None:
    if LAME is not None:
        subprocess.run(
            [LAME, "-m", "j", "-b", "48", str(source), str(dest)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if _first_mp3_channel_mode(dest) == 1:
            return
    ffmpeg = _require_ffmpeg()
    for bitrate in ("32k", "40k", "48k", "64k", "96k", "128k"):
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-codec:a",
                "libmp3lame",
                "-b:a",
                bitrate,
                "-joint_stereo",
                "1",
                str(dest),
            ],
            check=True,
        )
        if _first_mp3_channel_mode(dest) == 1:
            return
    pytest.fail("could not synthesize a joint-stereo MP3 fixture")


def _truncate_flac_after_metadata(path: Path, *, keep_fraction: float) -> None:
    data = path.read_bytes()
    if data[:4] != b"fLaC":
        raise ValueError(f"{path.name} is not FLAC")
    offset = 4
    while True:
        if offset + 4 > len(data):
            raise ValueError(f"{path.name} has truncated FLAC metadata")
        header = struct.unpack(">I", data[offset : offset + 4])[0]
        is_last = (header & 0x80000000) != 0
        block_len = header & 0x00FFFFFF
        offset += 4 + block_len
        if is_last:
            break
    metadata_end = offset
    audio_len = len(data) - metadata_end
    keep_audio = max(1, int(audio_len * keep_fraction))
    path.write_bytes(data[: metadata_end + keep_audio])


def _first_mp3_channel_mode(path: Path) -> int:
    data = path.read_bytes()
    sync = _find_mp3_sync(data, _id3v2_skip_size(data))
    if sync < 0 or sync + 4 > len(data):
        raise AssertionError(f"{path.name} has no MPEG sync")
    return (data[sync + 3] >> 6) & 0x03


def _transcode_vbr_mp3_no_xing(src: Path, dest: Path) -> None:
    ffmpeg = _require_ffmpeg()
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-write_xing",
            "0",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(dest),
        ],
        check=True,
    )


def _transcode_part(src: Path, dest: Path, codec: str) -> None:
    ffmpeg = _require_ffmpeg()
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
    ]
    if codec == "mp3":
        args.extend(["-codec:a", "libmp3lame", "-b:a", "128k", str(dest)])
    elif codec == "flac":
        args.extend(["-codec:a", "flac", str(dest)])
    else:
        raise ValueError(codec)
    subprocess.run(args, check=True)


def _manifest(stable_id: str, *, ext: str) -> dict:
    return {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "hdemucs_mmi", "version": "4.0.1"},
        "source": {
            "path": "/music/example.flac",
            "sha256": "a" * 64,
        },
        "files": {
            "vocals": f"vocals.{ext}",
            "drums": f"drums.{ext}",
            "bass": f"bass.{ext}",
            "other": f"other.{ext}",
        },
        "layout": "demucs4",
    }


def _bundle_with_codec(root: Path, stable_id: str, codec: str) -> Path:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    sine = bundle / "source.wav"
    _write_sine_wav(sine)
    ext = codec
    for part in ("vocals", "drums", "bass", "other"):
        _transcode_part(sine, bundle / f"{part}.{ext}", codec)
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id, ext=ext)),
        encoding="utf-8",
    )
    return bundle


def _bundle_with_silence_flac(root: Path, stable_id: str) -> Path:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    silence = bundle / "source.wav"
    _write_silence_wav(silence)
    for part in ("vocals", "drums", "bass", "other"):
        _transcode_part(silence, bundle / f"{part}.flac", "flac")
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id, ext="flac")),
        encoding="utf-8",
    )
    return bundle


def _wav_bundle(root: Path, stable_id: str = "track-wav") -> Path:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    for part in ("vocals", "drums", "bass", "other"):
        _write_wav(bundle / f"{part}.wav")
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id, ext="wav")),
        encoding="utf-8",
    )
    return bundle


def _client(stems_dir: Path) -> TestClient:
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_mp3_v1_bundle_loads_with_audio_mpeg(tmp_path: Path) -> None:
    _bundle_with_codec(tmp_path, "track-mp3", "mp3")
    bundle = load_stem_bundle("track-mp3", stems_dir=tmp_path)
    assert bundle.media_type == "audio/mpeg"
    assert bundle.alignment.sample_rate > 0
    assert bundle.alignment.frame_count > 0
    assert bundle.alignment.channels == 2


def test_flac_v1_bundle_loads(tmp_path: Path) -> None:
    _bundle_with_codec(tmp_path, "track-flac", "flac")
    bundle = load_stem_bundle("track-flac", stems_dir=tmp_path)
    assert bundle.media_type == "audio/flac"
    assert bundle.alignment.sample_rate == 44_100
    assert bundle.alignment.frame_count > 0


def test_wav_v1_bundle_still_loads_exactly(tmp_path: Path) -> None:
    _wav_bundle(tmp_path)
    bundle = load_stem_bundle("track-wav", stems_dir=tmp_path)
    assert bundle.media_type == "audio/wav"
    assert bundle.alignment == WavMetadata(
        sample_rate=44_100,
        frame_count=12,
        channels=2,
    )


def test_mixed_containers_raise(tmp_path: Path) -> None:
    bundle = _bundle_with_codec(tmp_path, "track-mixed", "mp3")
    _write_wav(bundle / "other.wav")
    manifest = _manifest("track-mixed", ext="mp3")
    manifest["files"]["other"] = "other.wav"
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(StemArtifactError, match="mixes containers"):
        load_stem_bundle("track-mixed", stems_dir=tmp_path)


def test_mismatched_sample_rate_raises(tmp_path: Path) -> None:
    bundle = _bundle_with_codec(tmp_path, "track-rate", "mp3")
    bad = bundle / "other-bad.wav"
    _write_wav(bad, sample_rate=22_050)
    _transcode_part(bad, bundle / "other.mp3", "mp3")
    with pytest.raises(StemArtifactError, match="sample_rate"):
        load_stem_bundle("track-rate", stems_dir=tmp_path)


def test_mp3_suffix_with_wav_bytes_raises(tmp_path: Path) -> None:
    bundle = _bundle_with_codec(tmp_path, "track-garbage", "mp3")
    _write_wav(bundle / "vocals.mp3")
    with pytest.raises(StemArtifactError, match="not MPEG"):
        load_stem_bundle("track-garbage", stems_dir=tmp_path)


def test_truncated_mp3_raises(tmp_path: Path) -> None:
    """[if] an MP3 part is truncated to 32 bytes [then] the bundle is rejected."""
    bundle = _bundle_with_codec(tmp_path, "track-trunc", "mp3")
    vocals = bundle / "vocals.mp3"
    vocals.write_bytes(vocals.read_bytes()[:32])
    with pytest.raises(StemArtifactError):
        load_stem_bundle("track-trunc", stems_dir=tmp_path)


def test_truncated_mp3_at_1000_bytes_raises(tmp_path: Path) -> None:
    """[if] an MP3 part is truncated to 1000 bytes [then] the bundle is rejected."""
    bundle = _bundle_with_codec(tmp_path, "track-trunc-1k", "mp3")
    vocals = bundle / "vocals.mp3"
    vocals.write_bytes(vocals.read_bytes()[:1000])
    with pytest.raises(StemArtifactError):
        load_stem_bundle("track-trunc-1k", stems_dir=tmp_path)


def test_truncated_mp3_at_ninety_percent_raises(tmp_path: Path) -> None:
    """[if] an MP3 part is truncated to 90% of its size [then] the bundle is rejected."""
    bundle = _bundle_with_codec(tmp_path, "track-trunc-90", "mp3")
    vocals = bundle / "vocals.mp3"
    original = vocals.read_bytes()
    vocals.write_bytes(original[: int(len(original) * 0.9)])
    with pytest.raises(StemArtifactError):
        load_stem_bundle("track-trunc-90", stems_dir=tmp_path)


def test_mp3_with_trailing_id3v1_tag_loads(tmp_path: Path) -> None:
    """[if] an MP3 part grows by a trailing ID3v1 tag [then] the bundle still loads."""
    bundle = _bundle_with_codec(tmp_path, "track-id3v1", "mp3")
    vocals = bundle / "vocals.mp3"
    vocals.write_bytes(vocals.read_bytes() + (b"TAG" + b"\x00" * 125))
    bundle = load_stem_bundle("track-id3v1", stems_dir=tmp_path)
    assert bundle.media_type == "audio/mpeg"


def test_truncated_flac_one_third_raises(tmp_path: Path) -> None:
    """[if] a FLAC part is truncated to one third of its size [then] the bundle is rejected."""
    bundle = _bundle_with_codec(tmp_path, "track-flac-trunc", "flac")
    vocals = bundle / "vocals.flac"
    _truncate_flac_after_metadata(vocals, keep_fraction=1 / 3)
    with pytest.raises(StemArtifactError):
        load_stem_bundle("track-flac-trunc", stems_dir=tmp_path)


def test_silence_flac_bundle_loads(tmp_path: Path) -> None:
    """[if] a FLAC bundle is generated from silence [then] the tiny bundle still loads."""
    _bundle_with_silence_flac(tmp_path, "track-silence-flac")
    bundle = load_stem_bundle("track-silence-flac", stems_dir=tmp_path)
    assert bundle.media_type == "audio/flac"
    assert bundle.alignment.frame_count > 0


def test_intact_mp3_bundle_still_loads(tmp_path: Path) -> None:
    """[if] an untouched MP3 v1 bundle is loaded [then] geometry validates and loads."""
    _bundle_with_codec(tmp_path, "track-intact-mp3", "mp3")
    bundle = load_stem_bundle("track-intact-mp3", stems_dir=tmp_path)
    assert bundle.media_type == "audio/mpeg"
    assert bundle.alignment.frame_count > 0


def test_intact_flac_bundle_still_loads(tmp_path: Path) -> None:
    """[if] an untouched FLAC v1 bundle is loaded [then] geometry validates and loads."""
    _bundle_with_codec(tmp_path, "track-intact-flac", "flac")
    bundle = load_stem_bundle("track-intact-flac", stems_dir=tmp_path)
    assert bundle.media_type == "audio/flac"
    assert bundle.alignment.frame_count > 0


def test_vbr_mp3_without_xing_matches_ffprobe(tmp_path: Path) -> None:
    """[if] a VBR MP3 is encoded without a Xing tag [then] scanned frame_count matches ffprobe."""
    source = tmp_path / "source.wav"
    _write_sine_wav(source)
    mp3 = tmp_path / "vbr-no-xing.mp3"
    _transcode_vbr_mp3_no_xing(source, mp3)
    metadata = read_mp3_metadata(mp3)
    expected = _ffprobe_sample_count(mp3)
    tolerance = int(FRAME_MISMATCH_TOL_S * metadata.sample_rate)
    assert abs(metadata.frame_count - expected) <= tolerance


def test_joint_stereo_mp3_reports_two_channels(tmp_path: Path) -> None:
    """[if] an MP3 part is joint stereo [then] read_mp3_metadata reports 2 channels."""
    source = tmp_path / "source.wav"
    _write_sine_wav(source)
    mp3 = tmp_path / "joint-stereo.mp3"
    _encode_joint_stereo_mp3(source, mp3)
    assert _first_mp3_channel_mode(mp3) == 1
    metadata = read_mp3_metadata(mp3)
    assert metadata.channels == 2


def test_mono_mp3_reports_one_channel(tmp_path: Path) -> None:
    """[if] an MP3 part is mono [then] read_mp3_metadata reports 1 channel."""
    ffmpeg = _require_ffmpeg()
    source = tmp_path / "source.wav"
    _write_sine_wav(source, duration_s=0.1)
    mp3 = tmp_path / "mono.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(mp3),
        ],
        check=True,
    )
    assert _first_mp3_channel_mode(mp3) == 3
    metadata = read_mp3_metadata(mp3)
    assert metadata.channels == 1


def test_http_manifest_and_part_stream_for_mp3_bundle(tmp_path: Path) -> None:
    _bundle_with_codec(tmp_path, "track-http", "mp3")
    with _client(tmp_path) as client:
        manifest = client.get("/api/v1/tracks/track-http/stems")
        stem = client.get("/api/v1/tracks/track-http/stems/vocals")
    assert manifest.status_code == 200
    body = manifest.json()
    assert body["schema"] == 1
    assert body["parts"]["vocals"]["media_type"] == "audio/mpeg"
    assert stem.status_code == 200
    assert stem.headers["content-type"].startswith("audio/mpeg")
    assert stem.content[:3] != b"RIFF"


def test_flac_blocksize_code_table_matches_spec() -> None:
    """[if] a FLAC frame header's blocksize code is 8-15 [then] the decoded block
    size follows the spec's uniform 256 << (code - 8) table for every code in that
    range [⛔️ if codes 12-15 fall back to a truncated 256 << (code - 12) base,
    which silently halves-or-worse the block size for the common 4096-sample
    libFLAC default (code 12) and every code above it]."""
    expected = {8: 256, 9: 512, 10: 1024, 11: 2048, 12: 4096, 13: 8192, 14: 16384, 15: 32768}
    for code, want in expected.items():
        result = _flac_block_size_from_header(
            code, b"", 0, min_blocksize=0, max_blocksize=0
        )
        assert result is not None
        block_size, offset = result
        assert block_size == want
        assert offset == 0


pytestmark = pytest.mark.rb_parity
