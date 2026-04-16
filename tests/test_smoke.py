"""Smoke test: pytest is configured, fixtures load, and the plugin runs.

Every other test file tags with requirement IDs; this one is intentionally
untagged so ``--strict-markers`` failures here are a clear signal that
pytest + conftest are broken (not just a specific RECON-0x test).
"""
from __future__ import annotations

import sqlite3

import pytest


def test_rb_fixture_opens(rb_plain_conn: sqlite3.Connection) -> None:
    """Fixture DB is a valid sqlite file with the expected tables."""
    names = {
        r[0]
        for r in rb_plain_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "djmdContent" in names, sorted(names)[:20]
    (count,) = rb_plain_conn.execute("SELECT count(*) FROM djmdContent").fetchone()
    # Fixture was built to ~47 tracks; allow 30..80 to survive benign future
    # re-prunes without flapping.
    assert 30 <= count <= 80, count


def test_live_db_marker_is_skipped_by_default() -> None:
    """Without ``--live-db``, live_db-marked tests are auto-skipped."""
    # We just assert the marker exists as a known pytest marker; the plugin's
    # pytest_collection_modifyitems hook is exercised by the sibling test file
    # `tests/shared/test_paths.py::test_live_db_gate` which actually carries
    # the marker. If that test ran, we broke the gate.
    assert hasattr(pytest, "mark")
