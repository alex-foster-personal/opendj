"""``apps.shared.engine_decode``: find ``odj-audio`` and read lengths with it."""

from __future__ import annotations

import os
import stat
import struct
import sys
import threading
import wave
from pathlib import Path

import pytest

from apps.shared import engine_decode
from apps.shared.engine_decode import (
    EngineDecodeFailed,
    EngineDecoderUnavailable,
    probe_duration_s,
    resolve_engine_decoder,
)

# The stand-in below is a shebang script, which Windows cannot execute.
_POSIX_STAND_IN = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the stand-in odj-audio is a POSIX shell script; Windows cannot run a shebang",
)


def _exe(path: Path, commands: str = "decode probe") -> Path:
    """A stand-in binary whose ``help`` lists ``commands`` the way odj-audio does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = "\\n".join(f"  odj-audio {c} PATH" for c in commands.split())
    path.write_text(f'#!/bin/sh\nprintf "usage:\\n{usage}\\n"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@_POSIX_STAND_IN
def test_the_env_override_wins_and_the_repo_build_is_not_read(tmp_path: Path) -> None:
    bundled = _exe(tmp_path / "payload" / "bin" / "odj-audio")
    _exe(tmp_path / "repo" / engine_decode.REPO_TARGET / "release" / engine_decode.EXE_NAME)
    got = resolve_engine_decoder({"ODJ_AUDIO_BIN": str(bundled)}, repo_root=tmp_path / "repo")
    assert got == bundled


@_POSIX_STAND_IN
def test_a_broken_override_is_the_answer_not_a_fallback(tmp_path: Path) -> None:
    _exe(tmp_path / "repo" / engine_decode.REPO_TARGET / "release" / engine_decode.EXE_NAME)
    plain = tmp_path / "odj-audio"
    plain.write_text("not executable")
    with pytest.raises(EngineDecoderUnavailable, match="ODJ_AUDIO_BIN"):
        resolve_engine_decoder({"ODJ_AUDIO_BIN": str(plain)}, repo_root=tmp_path / "repo")


@_POSIX_STAND_IN
def test_without_override_the_newest_repo_build_is_used(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    release = _exe(root / engine_decode.REPO_TARGET / "release" / engine_decode.EXE_NAME)
    debug = _exe(root / engine_decode.REPO_TARGET / "debug" / engine_decode.EXE_NAME)
    os.utime(release, (1_000, 1_000))
    os.utime(debug, (2_000, 2_000))
    assert resolve_engine_decoder({}, repo_root=root) == debug


@_POSIX_STAND_IN
def test_a_stale_build_without_the_subcommands_is_passed_over(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    fresh = _exe(root / engine_decode.REPO_TARGET / "release" / engine_decode.EXE_NAME)
    stale = _exe(root / engine_decode.REPO_TARGET / "debug" / engine_decode.EXE_NAME, "render serve")
    os.utime(fresh, (1_000, 1_000))
    os.utime(stale, (2_000, 2_000))
    # The newer debug build predates decode/probe, so the older release wins.
    assert resolve_engine_decoder({}, repo_root=root) == fresh
    with pytest.raises(EngineDecoderUnavailable, match="predates"):
        resolve_engine_decoder({"ODJ_AUDIO_BIN": str(stale)}, repo_root=root)


def test_no_binary_anywhere_raises(tmp_path: Path) -> None:
    with pytest.raises(EngineDecoderUnavailable, match="no ODJ_AUDIO_BIN"):
        resolve_engine_decoder({}, repo_root=tmp_path)


@pytest.fixture
def engine() -> Path:
    try:
        return resolve_engine_decoder()
    except EngineDecoderUnavailable as exc:
        pytest.skip(f"no odj-audio build in this checkout: {exc}")
        raise  # unreachable: skip raises; keeps the return type total for mypy


def test_probe_reads_the_stated_length(tmp_path: Path, engine: Path) -> None:
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x01" * 48000 * 2)
    assert probe_duration_s(path, engine) == 2.0


def test_probe_is_none_for_a_file_that_is_not_audio(tmp_path: Path, engine: Path) -> None:
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio" * 256)
    assert probe_duration_s(junk, engine) is None


def test_an_m4a_length_excludes_its_priming_frames(engine: Path) -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "apps/audio-engine/tests/fixtures/audio/click-250ms-aac.m4a"
    )
    assert probe_duration_s(fixture, engine) == 1.0


_M4A = (
    Path(__file__).resolve().parents[2]
    / "apps/audio-engine/tests/fixtures/audio/click-250ms-aac.m4a"
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.m4a", True), ("a.M4A", True), ("a.alac", True), ("a.mp3", False),
     ("a.FLAC", False), ("a.wav", False), ("a.aiff", False), ("a.ogg", False)],
)
def test_only_containers_libsndfile_cannot_read_take_the_engine(
    name: str, expected: bool
) -> None:
    assert engine_decode.needs_engine_decode(Path(name)) is expected


def test_an_m4a_decodes_to_its_frames_without_the_priming(engine: Path) -> None:
    pcm, rate, channels = engine_decode.decode_f32(_M4A, mono=True, exe=engine)
    assert (rate, channels, len(pcm)) == (44100, 1, 4 * 44100)
    peak = max(range(0, len(pcm), 4), key=lambda i: abs(struct.unpack_from("<f", pcm, i)[0]))
    # The click sits at 250 ms; priming left in would push it ~1024 frames late.
    assert abs(peak // 4 - 11025) < 64


def test_an_m4a_becomes_a_float_wav_a_plain_reader_opens(tmp_path: Path, engine: Path) -> None:
    soundfile = pytest.importorskip("soundfile")
    wav = engine_decode.decode_to_wav(_M4A, tmp_path / "click.wav", exe=engine)
    info = soundfile.info(str(wav))
    assert (info.samplerate, info.channels, info.frames, info.subtype) == (
        44100, 2, 44100, "FLOAT"
    )


@pytest.mark.parametrize("to_wav", [False, True])
def test_a_file_the_engine_cannot_read_is_a_decode_failure(
    tmp_path: Path, engine: Path, to_wav: bool
) -> None:
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"not audio" * 256)
    with pytest.raises(EngineDecodeFailed, match="could not decode"):
        if to_wav:
            engine_decode.decode_to_wav(junk, tmp_path / "junk.wav", exe=engine)
        else:
            engine_decode.decode_f32(junk, mono=True, exe=engine)


@pytest.mark.parametrize(
    ("nbytes", "ok"), [(4 * 2 * 10, True), (4 * 2 * 10 - 4, False), (0, False)]
)
def test_output_must_be_exactly_the_stated_frames(nbytes: int, ok: bool) -> None:
    # A decode cut short (killed, truncated pipe) must not pass as the track.
    summary = {"sample_rate": 44100, "channels": 2, "frames": 10}
    if ok:
        engine_decode._require_stated_length(nbytes, summary, _M4A)
    else:
        with pytest.raises(EngineDecodeFailed, match=f"{nbytes} bytes .* not the 10 frames"):
            engine_decode._require_stated_length(nbytes, summary, _M4A)


def test_a_failed_decode_leaves_no_partial_wav(tmp_path: Path, engine: Path) -> None:
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"not audio" * 256)
    wav = tmp_path / "junk.wav"
    with pytest.raises(EngineDecodeFailed):
        engine_decode.decode_to_wav(junk, wav, exe=engine)
    assert not wav.exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.mkfifo is POSIX-only, and a FIFO no writer opens is how this "
    "test makes the real engine block",
)
def test_a_decode_that_stalls_is_killed_at_the_timeout(tmp_path: Path, engine: Path) -> None:
    # The real engine opens a FIFO nobody writes to, so it blocks with its
    # stdout open, which is the stall the read loop cannot time out by itself.
    stalled = tmp_path / "stalled.m4a"
    os.mkfifo(stalled)
    wav = tmp_path / "x.wav"
    # Backstop: if the watchdog is broken, unblock the engine after 10s so the
    # test fails on the wrong error instead of hanging the suite.
    backstop = threading.Timer(10, lambda: os.close(os.open(stalled, os.O_WRONLY)))
    backstop.start()
    try:
        with pytest.raises(EngineDecodeFailed, match="within 1s"):
            engine_decode.decode_to_wav(stalled, wav, exe=engine, timeout_s=1)
    finally:
        backstop.cancel()
    assert not wav.exists()
