"""Tests-tree conftest -- cross-platform collection gate.

Distinct from the repo-root ``conftest.py`` (which registers the reqs
plugin -- coverage-matrix.md writer + ``--live-db`` gate). This module
owns ONE thing: skipping ``requires_darwin``-marked items off macOS.

Both this hook and the reqs plugin's own ``pytest_collection_modifyitems``
run -- pytest calls every registered implementation of a hook, it is not
a single-winner override.
"""
from __future__ import annotations

import sys

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``requires_darwin`` items when not running on macOS."""
    if sys.platform == "darwin":
        return
    skip_darwin = pytest.mark.skip(reason="macOS-only")
    for item in items:
        if "requires_darwin" in item.keywords:
            item.add_marker(skip_darwin)
