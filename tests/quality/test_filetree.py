"""FILETREE.md stays a true map of the tracked tree.

The checks live in scripts/ci_tree_hygiene.py, which ci.yml's scope job also runs on every
pull request (a docs-only change that adds a top-level path can break this test).

- if a tracked top-level entry is missing from FILETREE.md then broken
- if a tracked entry under apps/, ops/, tools/ or ci/ (or a folder under docs/) is missing then broken
- if FILETREE.md lists a path that is not tracked then broken
"""

from __future__ import annotations

from pathlib import Path

from scripts.ci_tree_hygiene import (
    filetree_listed,
    filetree_missing_mapped,
    filetree_missing_top_level,
    filetree_stale,
    tracked_paths,
    tree_entries,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_top_level_entry_is_listed() -> None:
    """if a tracked top-level entry is missing from FILETREE.md then broken"""
    missing = filetree_missing_top_level(tree_entries(tracked_paths(REPO_ROOT)), filetree_listed(REPO_ROOT))
    assert not missing, f"add these to FILETREE.md: {missing}"


def test_every_mapped_child_is_listed() -> None:
    """if a tracked entry in a fully mapped folder is missing then broken"""
    missing = filetree_missing_mapped(tree_entries(tracked_paths(REPO_ROOT)), filetree_listed(REPO_ROOT))
    assert not missing, f"add these to FILETREE.md: {missing}"


def test_every_listed_path_is_tracked() -> None:
    """if FILETREE.md lists a path that is not tracked then broken"""
    stale = filetree_stale(tree_entries(tracked_paths(REPO_ROOT)), filetree_listed(REPO_ROOT))
    assert not stale, f"remove or fix these FILETREE.md entries: {stale}"
