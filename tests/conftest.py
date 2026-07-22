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
import sys

import pytest

# Optional-dependency gates, resolved once at collection time. Absence is a
# SKIP (the extra is deliberately opt-in), never a silent pass or a failure.
_HAS_MUTAGEN: bool = importlib.util.find_spec("mutagen") is not None
_HAS_JOBLIB: bool = importlib.util.find_spec("joblib") is not None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``requires_*``-marked items whose platform/extra is absent."""
    skip_darwin = pytest.mark.skip(reason="macOS-only")
    skip_mutagen = pytest.mark.skip(reason="needs the tags extra (mutagen)")
    skip_joblib = pytest.mark.skip(reason="needs joblib")
    for item in items:
        if sys.platform != "darwin" and "requires_darwin" in item.keywords:
            item.add_marker(skip_darwin)
        if not _HAS_MUTAGEN and "requires_mutagen" in item.keywords:
            item.add_marker(skip_mutagen)
        if not _HAS_JOBLIB and "requires_joblib" in item.keywords:
            item.add_marker(skip_joblib)
