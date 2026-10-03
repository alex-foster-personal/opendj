"""The workers' route to ``odj-audio decode`` when there is no ffmpeg (STEM-50).

[if] ODJ_AUDIO_BIN names an executable [then] it is the decoder, even with a repo build present, [else stop].
[if] ODJ_AUDIO_BIN is set but wrong [then] refuse naming every remedy; no repo build stands in, [else stop].
[if] ODJ_AUDIO_BIN is unset [then] a local cargo build is used (checkout), [else stop].
[if] the payload launcher runs [then] ODJ_AUDIO_BIN is the bin/odj-audio the payload build stages, [else stop].
[if] odj-audio decodes a real MP3 [then] the WAV it wrote matches its summary and ffmpeg's decode, [else stop].
[if] odj-audio decode fails [then] OdjAudioDecodeError with its stderr, never a partial WAV, [else stop].

The resolver tests stand an executable file in for the engine, because the
resolver only asks "is this an executable file"; the decode tests build and
run the real ``odj-audio`` from this checkout.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.odj_audio_binary import BIN_ENV, EXE_NAME, REPO_TARGET
from apps.shared import odj_audio_decode as dec
from apps.shared import platform_paths
from tests.rust_build_env import build_audio_engine

pytestmark = pytest.mark.requirement("STEM-50")

MP3_FIXTURE = platform_paths.PROJECT_ROOT / "tests/fixtures/phase7-dedup/src-128.mp3"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sys.executable, path)
    path.chmod(0o755)
    return path


def _repo_with_build(root: Path) -> Path:
    """A repo root holding a local debug build of the engine."""
    return _executable(root / REPO_TARGET / "debug" / EXE_NAME)


def test_odj_audio_bin_wins_over_a_repo_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repo_with_build(repo)
    packaged = _executable(tmp_path / "payload" / "bin" / EXE_NAME)

    got = dec.resolve_odj_audio(MP3_FIXTURE, {BIN_ENV: str(packaged)}, repo)

    assert got == packaged


def test_a_wrong_odj_audio_bin_refuses_and_never_uses_a_repo_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repo_with_build(repo)
    missing = tmp_path / "payload" / "bin" / EXE_NAME

    with pytest.raises(dec.NoDecoderError) as raised:
        dec.resolve_odj_audio(MP3_FIXTURE, {BIN_ENV: str(missing)}, repo)

    message = str(raised.value)
    assert str(MP3_FIXTURE) in message
    assert str(missing) in message
    for remedy in ("ffmpeg", "MDT_FFMPEG", BIN_ENV, "pre-transcode"):
        assert remedy in message, remedy


def test_without_odj_audio_bin_a_checkout_uses_its_own_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    built = _repo_with_build(repo)

    assert dec.resolve_odj_audio(MP3_FIXTURE, {}, repo) == built


def test_without_odj_audio_bin_or_a_build_it_refuses(tmp_path: Path) -> None:
    with pytest.raises(dec.NoDecoderError, match=BIN_ENV):
        dec.resolve_odj_audio(MP3_FIXTURE, {}, tmp_path / "empty-repo")


def test_the_payload_launcher_points_workers_at_the_staged_engine() -> None:
    """The packaged layout: the worker inherits ODJ_AUDIO_BIN from the launcher,
    and it names the very path the payload build stages the engine at."""
    from scripts.build_engine_payload import AUDIO_ENGINE_RELATIVE, LAUNCHER_TEMPLATE

    assert AUDIO_ENGINE_RELATIVE == "bin/odj-audio"
    assert f'{BIN_ENV}="$payload/{AUDIO_ENGINE_RELATIVE}"' in LAUNCHER_TEMPLATE
    assert f"export {BIN_ENV}" in LAUNCHER_TEMPLATE


# ----- the real engine ---------------------------------------------------------


@pytest.fixture(scope="module")
def odj_audio() -> Path:
    return build_audio_engine(platform_paths.PROJECT_ROOT / "apps" / "audio-engine")


def _wav_f32(path: Path) -> tuple[int, int, bytes]:
    """(sample_rate, channels, data) of the float WAV odj-audio writes."""
    raw = path.read_bytes()
    assert raw[0:4] == b"RIFF" and raw[8:16] == b"WAVEfmt "
    fmt, channels, sample_rate = struct.unpack("<HHI", raw[20:28])
    assert fmt == 3, "IEEE float"
    assert raw[36:40] == b"data"
    (size,) = struct.unpack("<I", raw[40:44])
    assert len(raw) == 44 + size
    return sample_rate, channels, raw[44:]


def test_a_real_mp3_decodes_at_its_own_rate_and_channels(odj_audio: Path, tmp_path: Path) -> None:
    decoded = dec.decode_to_wav(MP3_FIXTURE, tmp_path, binary=odj_audio)

    assert decoded.path.parent == tmp_path
    assert (decoded.sample_rate, decoded.channels) == (22050, 1)
    sample_rate, channels, data = _wav_f32(decoded.path)
    assert (sample_rate, channels) == (22050, 1)
    assert len(data) == decoded.frames * channels * 4
    assert 2.9 < decoded.duration_s < 3.1, decoded.duration_s
    samples = struct.unpack(f"<{len(data) // 4}f", data)
    assert max(abs(s) for s in samples) > 0.01, "audio, not silence"

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return  # the comparison below needs ffmpeg; the decode itself did not
    reference = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(MP3_FIXTURE), "-f", "f32le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    # Same gapless trim and the same samples as the decoder it replaces.
    assert len(reference) == len(data)
    ref = struct.unpack(f"<{len(reference) // 4}f", reference)
    assert max(abs(a - b) for a, b in zip(ref, samples, strict=True)) < 1e-4


def test_a_failed_decode_raises_with_stderr_and_leaves_no_wav(
    odj_audio: Path, tmp_path: Path
) -> None:
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio at all")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with pytest.raises(dec.OdjAudioDecodeError, match="odj-audio decode failed"):
        dec.decode_to_wav(junk, out_dir, binary=odj_audio)

    assert list(out_dir.iterdir()) == []
    # Control: the same out dir takes a real decode.
    assert dec.decode_to_wav(MP3_FIXTURE, out_dir, binary=odj_audio).path.is_file()


def test_the_decode_route_reads_no_ffmpeg(odj_audio: Path, tmp_path: Path) -> None:
    """PATH with no ffmpeg at all, as in the installed app: still decodes."""
    bare = tmp_path / "bare"
    bare.mkdir()
    assert shutil.which("ffmpeg", path=str(bare)) is None
    env = {**os.environ, "PATH": str(bare), BIN_ENV: str(odj_audio)}
    env.pop("MDT_FFMPEG", None)
    code = (
        "import sys; from pathlib import Path; "
        "from apps.shared import odj_audio_decode as d; "
        "b = d.resolve_odj_audio(Path(sys.argv[1])); "
        "print(d.decode_to_wav(Path(sys.argv[1]), Path(sys.argv[2]), binary=b).frames)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(MP3_FIXTURE), str(tmp_path)],
        cwd=platform_paths.PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout.strip()) > 0


def test_decoded_wav_lives_in_a_temp_dir_that_is_gone_after(
    odj_audio: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workers' context: a system temp dir (never the app bundle) that is
    removed when the worker is done with the WAV."""
    import tempfile

    monkeypatch.setenv(BIN_ENV, str(odj_audio))
    with dec.decoded_wav(MP3_FIXTURE, prefix="odj-test-decode-") as decoded:
        held = decoded.path
        assert held.is_file()
        assert held.parent.parent.resolve() == Path(tempfile.gettempdir()).resolve()
        assert held.parent.name.startswith("odj-test-decode-")
    assert not held.parent.exists()
