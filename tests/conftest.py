"""Tests-tree conftest -- cross-platform collection gate.

Distinct from the repo-root ``conftest.py`` (which registers the reqs
plugin -- coverage-matrix.md writer + ``--live-db`` gate). This module
owns ONE thing: skipping ``requires_darwin``-marked items off macOS.

Both this hook and the reqs plugin's own ``pytest_collection_modifyitems``
run -- pytest calls every registered implementation of a hook, it is not
a single-winner override.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys

import pytest

# Optional-dependency gates, resolved once at collection time. Absence is a
# SKIP (the extra is deliberately opt-in), never a silent pass or a failure.
_HAS_MUTAGEN: bool = importlib.util.find_spec("mutagen") is not None
_HAS_JOBLIB: bool = importlib.util.find_spec("joblib") is not None
_HAS_AUDIO_STACK: bool = (
    importlib.util.find_spec("soundfile") is not None
    and importlib.util.find_spec("librosa") is not None
)
_HAS_FPCALC: bool = shutil.which("fpcalc") is not None
# madmom is not installable from PyPI on Python 3.10+ (0.16.1 imports the
# long-removed collections.MutableSequence), so requirements.txt pulls the
# git HEAD with --no-build-isolation and no pyproject extra can supply it.
# CI installs it and runs these tests; a plain `uv sync` venv cannot.
_HAS_MADMOM: bool = importlib.util.find_spec("madmom") is not None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``requires_*``-marked items whose platform/extra is absent."""
    skip_darwin = pytest.mark.skip(reason="macOS-only")
    skip_mutagen = pytest.mark.skip(reason="needs the tags extra (mutagen)")
    skip_joblib = pytest.mark.skip(reason="needs joblib")
    skip_madmom = pytest.mark.skip(
        reason="needs madmom (git HEAD; `pip install -r requirements.txt`)"
    )
    skip_audio = pytest.mark.skip(reason="needs the analysis extra (soundfile/librosa)")
    skip_fpcalc = pytest.mark.skip(reason="needs chromaprint's fpcalc on PATH")
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
