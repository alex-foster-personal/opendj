"""Shared fixtures for the Phase 6 analysis tests.

We synthesise audio in-memory so the suite has no external dependencies.
Fixtures land in ``tmp_path`` so nothing leaks between tests.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")

SR = 44100


def _click_track(bpm: float = 120.0, duration_s: float = 10.0, sr: int = SR) -> np.ndarray:
    """Synthetic click track with transients every beat at ``bpm``."""
    total = int(duration_s * sr)
    y = np.zeros(total, dtype=np.float32)
    beat_interval_s = 60.0 / bpm
    burst_len = int(0.01 * sr)
    t = 0.0
    while t < duration_s:
        idx = int(t * sr)
        end = min(total, idx + burst_len)
        env = np.linspace(1.0, 0.0, end - idx, dtype=np.float32)
        y[idx:end] = env
        t += beat_interval_s
    return y


def _sine(freq: float, duration_s: float, sr: int = SR) -> np.ndarray:
    t = np.arange(int(duration_s * sr), dtype=np.float32) / sr
    return (np.sin(2.0 * math.pi * freq * t).astype(np.float32) * 0.3)


@pytest.fixture(scope="session")
def click_120_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("analysis_fixtures")
    p = base / "click_120bpm.wav"
    sf.write(str(p), _click_track(120.0, 15.0), SR)
    return p


@pytest.fixture(scope="session")
def click_90_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("analysis_fixtures_90")
    p = base / "click_90bpm.wav"
    sf.write(str(p), _click_track(90.0, 12.0), SR)
    return p


@pytest.fixture()
def mp3_fixture(tmp_path: Path) -> Path:
    """1 s MP3 via ``lame`` (skip if not on PATH)."""
    p = tmp_path / "sample.mp3"
    wav = tmp_path / "_sample.wav"
    y = (np.random.default_rng(0).standard_normal(SR) * 0.05).astype(np.float32)
    sf.write(str(wav), y, SR)
    try:
        r = subprocess.run(
            ["lame", "-S", "--silent", str(wav), str(p)],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0 or not p.exists():
            pytest.skip("lame not available for mp3 fixture")
    except (FileNotFoundError, subprocess.TimeoutExpired):  # pragma: no cover
        pytest.skip("lame not available for mp3 fixture")
    return p


@pytest.fixture()
def flac_fixture(tmp_path: Path) -> Path:
    """1 s FLAC with a 440 Hz sine."""
    p = tmp_path / "sample.flac"
    sf.write(str(p), _sine(440.0, 1.0), SR, format="FLAC")
    return p


@pytest.fixture()
def ogg_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "sample.ogg"
    try:
        sf.write(str(p), _sine(440.0, 1.0), SR, format="OGG", subtype="VORBIS")
    except Exception:
        pytest.skip("libsndfile OGG/VORBIS support missing")
    return p


@pytest.fixture()
def m4a_fixture(tmp_path: Path) -> Path:
    """M4A via ``ffmpeg`` (skip if not on PATH)."""
    p = tmp_path / "sample.m4a"
    wav = tmp_path / "_sample.wav"
    sf.write(str(wav), _sine(440.0, 1.0), SR)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
             "-c:a", "aac", "-b:a", "96k", str(p)],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0 or not p.exists():
            pytest.skip("ffmpeg not available for m4a fixture")
    except (FileNotFoundError, subprocess.TimeoutExpired):  # pragma: no cover
        pytest.skip("ffmpeg not available for m4a fixture")
    return p
