"""scripts/stem_bundle_worker.py in an install with no ffmpeg (STEM-50, STEM-51).

The packaged app ships no ffmpeg (packaged check of 316572f5, Fri 2 Oct 2026,
finding 3: every MP3 failed with "non-WAV input ... needs ffmpeg on PATH").

[if] no ffmpeg and odj-audio resolves [then] a non-WAV source passes the decoder preflight, [else stop].
[if] no ffmpeg and no odj-audio [then] the preflight refuses naming both, before the model loads, [else stop].
[if] ffmpeg resolves [then] nothing changes: the policy codec and ffmpeg decode stand, [else stop].
[if] the policy codec is mp3 and no ffmpeg [then] parts are FLAC and the reason is recorded, [else stop].

These are the worker's pure decisions, run without torch (it never enters the
repo venv). The decode itself is ``odj-audio decode``, exercised for real in
tests/shared/test_odj_audio_decode.py.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import scripts.stem_bundle_worker as worker
from apps.shared.odj_audio_decode import NoDecoderError

pytestmark = pytest.mark.requirement("STEM-50")

MP3 = Path("/music/track.mp3")


@pytest.fixture
def no_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH holding no ffmpeg and no MDT_FFMPEG, as in the installed app."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("PATH", str(bare))
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    assert shutil.which("ffmpeg") is None


def _engine(tmp_path: Path) -> Path:
    engine = tmp_path / "payload" / "bin" / "odj-audio"
    engine.parent.mkdir(parents=True)
    shutil.copy2(sys.executable, engine)
    engine.chmod(0o755)
    return engine


def test_no_ffmpeg_with_odj_audio_passes_the_decoder_preflight(
    no_ffmpeg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(_engine(tmp_path)))

    assert worker._ffmpeg_reachable() is False
    worker._preflight_decoder(MP3)


def test_no_ffmpeg_and_no_odj_audio_refuses_naming_both(
    no_ffmpeg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(tmp_path / "no-such-odj-audio"))

    with pytest.raises(NoDecoderError) as raised:
        worker._preflight_decoder(MP3)

    message = str(raised.value)
    for named in (str(MP3), "ffmpeg", "MDT_FFMPEG", "ODJ_AUDIO_BIN", "pre-transcode"):
        assert named in message, named
    # Control: a WAV needs neither, so the same environment lets it through.
    worker._preflight_decoder(Path("/music/track.wav"))


@pytest.mark.requirement("STEM-51")
def test_mp3_policy_writes_flac_and_says_why_without_ffmpeg(no_ffmpeg: None) -> None:
    codec, why = worker._effective_output_codec("mp3")

    assert codec == "flac"
    assert why is not None and "ffmpeg" in why and "mp3" in why
    # Control: a lossless source's policy codec is already FLAC, no fallback.
    assert worker._effective_output_codec("flac") == ("flac", None)


def test_with_ffmpeg_nothing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MDT_FFMPEG alone (the worker trusts it as given) keeps the dev path."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("PATH", str(bare))
    monkeypatch.setenv("MDT_FFMPEG", "/opt/ffmpeg/bin/ffmpeg")
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(tmp_path / "no-such-odj-audio"))

    assert worker._ffmpeg_reachable() is True
    assert worker._effective_output_codec("mp3") == ("mp3", None)
    # odj-audio is not consulted at all when ffmpeg resolves.
    worker._preflight_decoder(MP3)


class _CpuTensor:
    """The ``.detach().cpu().numpy()`` subset of a torch tensor the encoder
    calls, over a real numpy array (torch never enters the repo venv)."""

    def __init__(self, array: object) -> None:
        self._array = array

    def detach(self) -> _CpuTensor:
        return self

    def cpu(self) -> _CpuTensor:
        return self

    def numpy(self) -> object:
        return self._array


@pytest.mark.requirement("STEM-51")
@pytest.mark.requires_audio_stack
def test_the_fallback_codec_encodes_with_no_ffmpeg_at_all(no_ffmpeg: None, tmp_path: Path) -> None:
    """The real encode chain, with no ffmpeg anywhere, writes a FLAC part that
    decodes to the frames it was given: the job finishes instead of dying at
    the encode after the separation already ran."""
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    rate, frames = 44100, 44100
    t = np.arange(frames, dtype=np.float32) / rate
    tone = (0.25 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    codec, _why = worker._effective_output_codec("mp3")
    out = tmp_path / f"vocals.{codec}"

    encoded, rung2 = worker._encode_stem_part(
        _CpuTensor(np.stack([tone, tone])),
        rate,
        codec=codec,
        mp3_settings=None,
        source_bytes=1_000_000,
        source_kbps=320.0,
        out_path=out,
    )

    assert rung2 is None
    assert out.read_bytes() == encoded
    info = sf.info(str(out))
    assert (info.format, info.samplerate, info.channels, info.frames) == ("FLAC", rate, 2, frames)
