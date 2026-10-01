"""v1 stem bundles must load MP3 and FLAC parts by their own container."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.stems.artifacts import (
    FRAME_MISMATCH_TOL_S,
    StemArtifactError,
    WavMetadata,
    _find_mp3_sync,
    _flac_block_size_from_header,
    _flac_valid_frame_header_at,
    _id3v2_skip_size,
    _validate_mp3_xing_tail,
    load_stem_bundle,
    read_mp3_metadata,
)
from apps.webui.server.routes.stems import router

REAL_MP3_BUNDLE = Path(
    "/private/tmp/claude-502/-Users-dev-code-music-dj-tools-lanes/"
    "ebd907a5-4922-486d-b381-b825be5f341b/scratchpad/realstems/"
    "002acb181dceb41f9efc5ceb11b1d16560918f84"
)
REAL_FLAC_BUNDLE = Path(
    "/private/tmp/claude-502/-Users-dev-code-music-dj-tools-lanes/"
    "ebd907a5-4922-486d-b381-b825be5f341b/scratchpad/realstems/"
    "331f7de0240e1abf924f5f9d0b183ff1f2c86cfa"
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
    pytest.skip("UNAVAILABLE: installed MP3 encoders cannot synthesize joint stereo")


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


def _require_real_mp3_bundle() -> Path:
    if not REAL_MP3_BUNDLE.is_dir():
        pytest.skip(f"real MP3 stem bundle missing at {REAL_MP3_BUNDLE}")
    return REAL_MP3_BUNDLE


def _require_real_flac_bundle() -> Path:
    if not REAL_FLAC_BUNDLE.is_dir():
        pytest.skip(f"real FLAC stem bundle missing at {REAL_FLAC_BUNDLE}")
    return REAL_FLAC_BUNDLE


def _copy_real_mp3_bundle(root: Path, stable_id: str) -> Path:
    source = _require_real_mp3_bundle()
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    for part in ("vocals", "drums", "bass", "other"):
        shutil.copy2(source / f"{part}.mp3", bundle / f"{part}.mp3")
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id, ext="mp3")),
        encoding="utf-8",
    )
    return bundle


def _encode_mp3_with_duration(dest: Path, *, duration_s: float, sample_rate: int = 44_100) -> None:
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
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(dest),
        ],
        check=True,
    )


def _bundle_with_chained_mp3_tolerance(root: Path, stable_id: str) -> Path:
    """Four MP3 parts whose frame counts step down within tol of the previous part only."""
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    sample_rate = 44_100
    samples_per_frame = 1152
    tol_frames = int(FRAME_MISMATCH_TOL_S * sample_rate)
    step_samples = tol_frames - samples_per_frame
    assert step_samples > 0
    base_duration_s = 30.0
    durations = [
        base_duration_s,
        base_duration_s - step_samples / sample_rate,
        base_duration_s - 2 * step_samples / sample_rate,
        base_duration_s - 3 * step_samples / sample_rate,
    ]
    frame_counts: list[int] = []
    for part, duration in zip(("vocals", "drums", "bass", "other"), durations, strict=True):
        dest = bundle / f"{part}.mp3"
        _encode_mp3_with_duration(dest, duration_s=duration, sample_rate=sample_rate)
        frame_counts.append(read_mp3_metadata(dest).frame_count)
    gaps = [
        frame_counts[index - 1] - frame_counts[index]
        for index in range(1, len(frame_counts))
    ]
    assert all(0 < gap < tol_frames for gap in gaps), gaps
    assert frame_counts[0] - frame_counts[-1] > tol_frames
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id, ext="mp3")),
        encoding="utf-8",
    )
    return bundle


def _bundle_with_aligned_mp3_tolerance(root: Path, stable_id: str) -> Path:
    """Four MP3 parts that all stay within tolerance of the reference part."""
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    sample_rate = 44_100
    tol_frames = int(FRAME_MISMATCH_TOL_S * sample_rate)
    base_duration_s = 20.0
    # Small enough that 3 steps plus MP3 frame-boundary rounding (1152 samples per
    # frame) still lands every part within tol_frames of the reference part.
    small_step_s = (tol_frames // 6) / sample_rate
    durations = [
        base_duration_s,
        base_duration_s - small_step_s,
        base_duration_s - 2 * small_step_s,
        base_duration_s - 3 * small_step_s,
    ]
    reference_count: int | None = None
    for part, duration in zip(("vocals", "drums", "bass", "other"), durations, strict=True):
        dest = bundle / f"{part}.mp3"
        _encode_mp3_with_duration(dest, duration_s=duration, sample_rate=sample_rate)
        count = read_mp3_metadata(dest).frame_count
        if reference_count is None:
            reference_count = count
        else:
            assert abs(count - reference_count) <= tol_frames
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id, ext="mp3")),
        encoding="utf-8",
    )
    return bundle


def _read_flac_streaminfo(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:4] != b"fLaC":
        raise ValueError(f"{path.name} is not FLAC")
    offset = 4
    while True:
        header = struct.unpack(">I", data[offset : offset + 4])[0]
        is_last = (header & 0x80000000) != 0
        block_type = (header & 0x7F000000) >> 24
        block_len = header & 0x00FFFFFF
        offset += 4
        payload = data[offset : offset + block_len]
        offset += block_len
        if block_type == 0:
            return payload
        if is_last:
            break
    raise ValueError(f"{path.name} FLAC missing STREAMINFO block")


def _find_flac_final_frame_header_end(path: Path) -> int:
    """Locate the last frame header end for a FLAC whose tail covers STREAMINFO."""
    data = path.read_bytes()
    streaminfo = _read_flac_streaminfo(path)
    min_blocksize = struct.unpack(">H", streaminfo[0:2])[0]
    max_blocksize = struct.unpack(">H", streaminfo[2:4])[0]
    packed = int.from_bytes(streaminfo[10:18], "big")
    total_samples = packed & 0xFFFFFFFFF
    file_size = len(data)
    tail_len = min(file_size, 65_536)
    tail = data[file_size - tail_len : file_size]
    for index in range(len(tail) - 1):
        if tail[index] != 0xFF or tail[index + 1] not in {0xF8, 0xF9}:
            continue
        validated = _flac_valid_frame_header_at(
            tail,
            index,
            min_blocksize=min_blocksize,
            max_blocksize=max_blocksize,
        )
        if validated is None or validated[0] != total_samples:
            continue
        return file_size - tail_len + validated[1]
    raise AssertionError(f"{path.name} has no final-frame header in its tail")


def _copy_real_flac_bundle(
    root: Path,
    stable_id: str,
    *,
    corrupt_vocals: Path | None = None,
) -> Path:
    source = _require_real_flac_bundle()
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    for part in ("vocals", "drums", "bass", "other"):
        src = corrupt_vocals if part == "vocals" and corrupt_vocals is not None else source / f"{part}.flac"
        shutil.copy2(src, bundle / f"{part}.flac")
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id, ext="flac")),
        encoding="utf-8",
    )
    return bundle


def _zero_mp3_tail_in_place(path: Path, *, overwrite_bytes: int = 3000) -> None:
    data = bytearray(path.read_bytes())
    id3_end = _id3v2_skip_size(data)
    sync = _find_mp3_sync(data, id3_end)
    assert sync >= 0
    start = max(sync + 4, len(data) - overwrite_bytes)
    data[start:] = b"\x00" * (len(data) - start)
    path.write_bytes(data)


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


def test_chained_mp3_alignment_tolerance_raises(tmp_path: Path) -> None:
    """[if] four MP3 parts step down within tol of the previous part only [then] the bundle is rejected."""
    _bundle_with_chained_mp3_tolerance(tmp_path, "track-chained-mp3")
    with pytest.raises(StemArtifactError, match="frame_count"):
        load_stem_bundle("track-chained-mp3", stems_dir=tmp_path)


def test_fixed_reference_mp3_alignment_loads_with_min_frames(tmp_path: Path) -> None:
    """[if] every MP3 part stays within tol of the reference part [then] alignment uses the minimum frame_count."""
    bundle = _bundle_with_aligned_mp3_tolerance(tmp_path, "track-aligned-mp3")
    loaded = load_stem_bundle("track-aligned-mp3", stems_dir=tmp_path)
    counts = [
        read_mp3_metadata(bundle / f"{part}.mp3").frame_count
        for part in ("vocals", "drums", "bass", "other")
    ]
    assert loaded.alignment.frame_count == min(counts)


def test_flac_truncated_after_final_frame_header_raises(tmp_path: Path) -> None:
    """[if] a FLAC part ends right after its final frame header [then] the bundle is rejected."""
    source = _require_real_flac_bundle() / "vocals.flac"
    corrupted = tmp_path / "vocals-trunc.flac"
    shutil.copy2(source, corrupted)
    header_end = _find_flac_final_frame_header_end(corrupted)
    corrupted.write_bytes(corrupted.read_bytes()[:header_end])
    _copy_real_flac_bundle(
        tmp_path,
        "track-flac-header-only",
        corrupt_vocals=corrupted,
    )
    with pytest.raises(StemArtifactError, match="vocals.flac"):
        load_stem_bundle("track-flac-header-only", stems_dir=tmp_path)


def test_zero_padded_real_mp3_tail_raises(tmp_path: Path) -> None:
    """[if] a real Xing MP3 keeps its length but zeroes its tail audio [then] the bundle is rejected."""
    bundle = _copy_real_mp3_bundle(tmp_path, "track-zero-tail")
    _zero_mp3_tail_in_place(bundle / "other.mp3")
    with pytest.raises(StemArtifactError, match="other.mp3"):
        load_stem_bundle("track-zero-tail", stems_dir=tmp_path)


def test_real_mp3_bundle_still_loads_after_xing_tail_check(tmp_path: Path) -> None:
    """[if] an unmodified real MP3 v1 bundle is loaded [then] it still validates after the tail check."""
    _copy_real_mp3_bundle(tmp_path, "track-real-mp3")
    bundle = load_stem_bundle("track-real-mp3", stems_dir=tmp_path)
    assert bundle.media_type == "audio/mpeg"
    assert bundle.alignment.frame_count > 0


def test_mp3_xing_tail_check_is_constant_cost(tmp_path: Path) -> None:
    """[if] the Xing tail check runs on a real MP3 part [then] it completes in under 20ms."""
    source = _require_real_mp3_bundle() / "other.mp3"
    part = tmp_path / "other.mp3"
    shutil.copy2(source, part)
    data = part.read_bytes()
    sync = _find_mp3_sync(data, _id3v2_skip_size(data))
    header = data[sync : sync + 4]
    version_id = (header[1] >> 3) & 0x03
    started = time.perf_counter()
    _validate_mp3_xing_tail(data, file_name=part.name, version_id=version_id)
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(f"mp3 xing tail check elapsed_ms={elapsed_ms:.3f}")
    assert elapsed_ms < 20.0


pytestmark = pytest.mark.rb_parity
