"""Tests for apps.shared.state.db -- connection leak regression."""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from apps.shared.state import db


class _SpyConnection:
    """Thin wrapper around a real sqlite3.Connection that tracks close()."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.close_called = False

    def close(self) -> None:
        self.close_called = True
        self._real.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class TestOpenRwConnLeak:
    """If _apply_rw_pragmas or apply_migrations raises, conn must be closed."""

    def test_conn_closed_when_pragmas_raise(self, tmp_path: object) -> None:
        """if open_rw pragma application raises then conn is leaked - broken"""
        db_path = tmp_path / "test.db"  # type: ignore[operator]
        spy: _SpyConnection | None = None

        original_connect = sqlite3.connect

        def patched_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal spy
            real = original_connect(*args, **kwargs)
            spy = _SpyConnection(real)
            return spy

        with (
            patch.object(db.sqlite3, "connect", side_effect=patched_connect),
            patch.object(db, "_apply_rw_pragmas", side_effect=RuntimeError("WAL fail")),
        ):
            with pytest.raises(RuntimeError, match="WAL fail"):
                db.open_rw(db_path)
            assert spy is not None
            assert spy.close_called, "conn.close() was never called after pragma failure"

    def test_conn_closed_when_migrations_raise(self, tmp_path: object) -> None:
        """if open_rw migration raises then conn is leaked - broken"""
        db_path = tmp_path / "test.db"  # type: ignore[operator]
        spy: _SpyConnection | None = None

        original_connect = sqlite3.connect

        def patched_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal spy
            real = original_connect(*args, **kwargs)
            spy = _SpyConnection(real)
            return spy

        with (
            patch.object(db.sqlite3, "connect", side_effect=patched_connect),
            patch.object(
                db._schema,
                "apply_migrations",
                side_effect=RuntimeError("migration fail"),
            ),
        ):
            with pytest.raises(RuntimeError, match="migration fail"):
                db.open_rw(db_path)
            assert spy is not None
            assert spy.close_called, "conn.close() was never called after migration failure"

    def test_conn_returned_on_success(self, tmp_path: object) -> None:
        """if open_rw succeeds then a valid connection is returned"""
        db_path = tmp_path / "test.db"  # type: ignore[operator]
        conn = db.open_rw(db_path, apply_schema=False)
        try:
            assert conn is not None
            conn.execute("SELECT 1")
        finally:
            conn.close()
