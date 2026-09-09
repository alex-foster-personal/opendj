"""Tests-tree conftest -- cross-platform collection gate.

Distinct from the repo-root ``conftest.py`` (which registers the reqs
plugin -- coverage-matrix.md writer + ``--live-db`` gate). This module
owns collection-time test gates, including the marker-owned Rekordbox parity
suite and skipping ``requires_darwin``-marked items off macOS.

Both this hook and the reqs plugin's own ``pytest_collection_modifyitems``
run -- pytest calls every registered implementation of a hook, it is not
a single-winner override.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _can_import(module_name: str) -> bool:
    """Return whether an optional dependency is actually importable."""
    try:
        __import__(module_name)
    except Exception:  # noqa: BLE001 - optional package imports may be broken.
        return False
    return True


# Optional-dependency gates, resolved once at collection time. Absence is a
# SKIP (the extra is deliberately opt-in), never a silent pass or a failure.
_HAS_MUTAGEN: bool = importlib.util.find_spec("mutagen") is not None
_HAS_JOBLIB: bool = importlib.util.find_spec("joblib") is not None
_HAS_AUDIO_STACK: bool = (
    _can_import("soundfile") and _can_import("librosa")
)
_HAS_FPCALC: bool = shutil.which("fpcalc") is not None
# ffmpeg is the local waveform decoder (apps.webui.server.rb_vendor_pkg.
# local_waveform). Absence is a real, tested runtime state -- the tests that
# assert what a MISSING ffmpeg produces run everywhere; only the ones that
# need a real decode are gated here.
_HAS_FFMPEG: bool = shutil.which("ffmpeg") is not None


def _ffmpeg_can_resample() -> bool:
    """True when this host's ffmpeg can actually run the pinned soxr chain."""
    from apps.analysis.pcm_fingerprint import (
        FingerprintUnavailable,
        require_resampler,
    )

    try:
        require_resampler()
    except FingerprintUnavailable:
        return False
    return True


# soxr is a BUILD option of ffmpeg, not a runtime flag: every build accepts
# `resampler=soxr` as an option value and a build without libsoxr then fails
# at filter-configure time. decode_fingerprint is defined over that
# resampler, so the probe RUNS the filter chain rather than reading a
# version string (Homebrew ffmpeg 9.0.1 on macOS passes the first check and
# fails the second, measured Wed 9 Sep 2026).
_HAS_SOXR: bool = _ffmpeg_can_resample() if _HAS_FFMPEG else False
# madmom is not installable from PyPI on Python 3.10+ (0.16.1 imports the
# long-removed collections.MutableSequence), so requirements.txt pulls the
# git HEAD with --no-build-isolation and no pyproject extra can supply it.
# CI installs it and runs these tests; a plain `uv sync` venv cannot.
_HAS_MADMOM: bool = importlib.util.find_spec("madmom") is not None


def _log_ci_venv_probe(phase: str) -> None:
    """Record the test interpreter and a non-preloading audio import probe."""
    probe = (
        "try:\n"
        "    import soundfile\n"
        "except Exception as error:\n"
        "    print(f'soundfile=ERROR: {type(error).__name__}: {error}')\n"
        "else:\n"
        "    print('soundfile=OK')\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=False,
            text=True,
        )
        result = (completed.stdout or completed.stderr).strip().replace("\n", " | ")
        result = result or f"soundfile=ERROR: subprocess exit {completed.returncode}"
    except OSError as error:
        result = f"soundfile=ERROR: {type(error).__name__}: {error}"
    print(
        "CI_VENV_PROBE "
        f"phase={phase} pid={os.getpid()} executable={sys.executable!r} "
        f"prefix={sys.prefix!r} venv_exists={(Path.cwd() / '.venv').is_dir()} {result}",
        flush=True,
    )


def _has_rb_parity_marker(path: Path) -> bool:
    """Return whether a test module belongs to the focused parity gate.

    Every tests/webui test_*.py is gate-owned: the focused-gate module
    enrolls them all by rglob, so a newly added one must be collected (and
    then marker-selected by tests/webui/conftest.py) with no hand-written
    marker (issue #1140). Any other module must declare the marker in source
    to be gate-owned.
    """
    if not (path.name.startswith("test_") and path.suffix == ".py"):
        return False
    if path.is_relative_to(Path(__file__).resolve().parent / "webui"):
        return True
    return "pytest.mark.rb_parity" in path.read_text(encoding="utf-8")


def _contains_rb_parity_marker(path: Path) -> bool:
    """Return whether a collection path contains a marker-owned test module."""
    if path.is_file():
        return _has_rb_parity_marker(path)
    if path.is_dir():
        return any(_has_rb_parity_marker(module) for module in path.rglob("test_*.py"))
    return False


def pytest_ignore_collect(
    collection_path: Path,
    config: pytest.Config,
) -> bool | None:
    """Avoid importing unowned modules when the focused marker gate runs."""
    mark_expression = str(getattr(config.option, "markexpr", "") or "").strip()
    if mark_expression != "rb_parity":
        return None
    return not _contains_rb_parity_marker(Path(str(collection_path)))


def pytest_collection_finish(session: pytest.Session) -> None:
    """Snapshot the environment after collection chose optional-dependency skips."""
    _log_ci_venv_probe("collection")


def pytest_runtestloop(session: pytest.Session) -> None:
    """Snapshot the same environment immediately before test-body execution."""
    _log_ci_venv_probe("execution")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``requires_*``-marked items whose platform/extra is absent."""
    skip_darwin = pytest.mark.skip(reason="macOS-only")
    skip_mutagen = pytest.mark.skip(reason="needs the tags extra (mutagen)")
    skip_joblib = pytest.mark.skip(reason="needs joblib")
    skip_madmom = pytest.mark.skip(
        reason="needs madmom (git HEAD; `pip install -r requirements.txt`)"
    )
    # UNAVAILABLE, not "fine": the analysis extra gates PARITY-03's only
    # end-to-end acceptance test, the one proving a real drain decodes audio
    # and writes a canonical analysis row. CI's pytest lane installs
    # requirements.txt, so librosa and soundfile are always present there and
    # that test really does gate every PR. On a bare `uv sync` dev venv it is
    # unmeasured, and the reason has to say so rather than read as a pass.
    skip_audio = pytest.mark.skip(
        reason="UNAVAILABLE: needs the analysis extra (soundfile/librosa), so "
               "the end-to-end drain is unmeasured here. This is a capability "
               "report, not a pass. CI installs it via requirements.txt."
    )
    skip_fpcalc = pytest.mark.skip(reason="needs chromaprint's fpcalc on PATH")
    skip_ffmpeg = pytest.mark.skip(reason="needs ffmpeg on PATH")
    skip_soxr = pytest.mark.skip(
        reason="UNAVAILABLE: this host's ffmpeg has no libsoxr, so the "
               "canonical decode fingerprint is unmeasured here. This is a "
               "capability report, not a pass."
    )
    for item in items:
        if sys.platform != "darwin" and "requires_darwin" in item.keywords:
            item.add_marker(skip_darwin)
        if not _HAS_MUTAGEN and "requires_mutagen" in item.keywords:
            item.add_marker(skip_mutagen)
        if not _HAS_JOBLIB and "requires_joblib" in item.keywords:
            item.add_marker(skip_joblib)
        if not _HAS_MADMOM and "requires_madmom" in item.keywords:
            item.add_marker(skip_madmom)
        if not _HAS_AUDIO_STACK and "requires_audio_stack" in item.keywords:
            item.add_marker(skip_audio)
        if not _HAS_FPCALC and "requires_fpcalc" in item.keywords:
            item.add_marker(skip_fpcalc)
        if not _HAS_FFMPEG and "requires_ffmpeg" in item.keywords:
            item.add_marker(skip_ffmpeg)
        if not _HAS_SOXR and "requires_soxr" in item.keywords:
            item.add_marker(skip_soxr)
