"""close_db_quietly must not swallow Rekordbox close failures (issue #8110)."""

from __future__ import annotations

import pytest

from apps.audit.rekordbox_db_util import close_db_quietly


class _FakeDb:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.closed = False
        self._exc = exc

    def close(self) -> None:
        self.closed = True
        if self._exc is not None:
            raise self._exc


def test_close_db_quietly_propagates_close_failure() -> None:
    """[if] db.close raises [then] close_db_quietly re-raises that error, [else stop]."""
    db = _FakeDb(RuntimeError("lock busy"))
    with pytest.raises(RuntimeError, match="lock busy"):
        close_db_quietly(db)
    assert db.closed is True


def test_close_db_quietly_closes_when_close_succeeds() -> None:
    """[if] db.close succeeds [then] close_db_quietly returns None, [else stop]."""
    db = _FakeDb()
    assert close_db_quietly(db) is None
    assert db.closed is True
