"""``apps.shared.engine_decode``: find ``odj-audio`` and read lengths with it."""

from __future__ import annotations

import os
import stat
import wave
from pathlib import Path

import pytest

from apps.shared import engine_decode
from apps.shared.engine_decode import (
    EngineDecoderUnavailable,
    probe_duration_s,
    resolve_engine_decoder,
)


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_the_env_override_wins_and_the_repo_build_is_not_read(tmp_path: Path) -> None:
    bundled = _exe(tmp_path / "payload" / "bin" / "odj-audio")
    _exe(tmp_path / "repo" / engine_decode.REPO_TARGET / "release" / engine_decode.EXE_NAME)
    got = resolve_engine_decoder({"ODJ_AUDIO_BIN": str(bundled)}, repo_root=tmp_path / "repo")
    assert got == bundled


def test_a_broken_override_is_the_answer_not_a_fallback(tmp_path: Path) -> None:
    _exe(tmp_path / "repo" / engine_decode.REPO_TARGET / "release" / engine_decode.EXE_NAME)
    plain = tmp_path / "odj-audio"
    plain.write_text("not executable")
    with pytest.raises(EngineDecoderUnavailable, match="ODJ_AUDIO_BIN"):
        resolve_engine_decoder({"ODJ_AUDIO_BIN": str(plain)}, repo_root=tmp_path / "repo")


def test_without_override_the_newest_repo_build_is_used(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    release = _exe(root / engine_decode.REPO_TARGET / "release" / engine_decode.EXE_NAME)
    debug = _exe(root / engine_decode.REPO_TARGET / "debug" / engine_decode.EXE_NAME)
    os.utime(release, (1_000, 1_000))
    os.utime(debug, (2_000, 2_000))
    assert resolve_engine_decoder({}, repo_root=root) == debug


def test_no_binary_anywhere_raises(tmp_path: Path) -> None:
    with pytest.raises(EngineDecoderUnavailable, match="no ODJ_AUDIO_BIN"):
        resolve_engine_decoder({}, repo_root=tmp_path)


@pytest.fixture
def engine() -> Path:
    try:
        return resolve_engine_decoder()
    except EngineDecoderUnavailable as exc:
        pytest.skip(f"no odj-audio build in this checkout: {exc}")


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
