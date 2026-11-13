"""SYNC-12969: Rekordbox rating batches commit once and keep readback verify.

[if] a bulk Rekordbox rating apply runs [then] it commits once and reads back every row, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.apply_ratings import live_run
from apps.sync.safety import SafetyAbort

# Live-write MECHANICS against tmp fixtures only; never a real rb target.
pytestmark = [pytest.mark.requirement("SYNC-12969"), pytest.mark.rekordbox_writeback]


class _Content:
    """Duck-typed stand-in for a pyrekordbox ``DjmdContent`` row."""

    def __init__(self, rating: int = 0) -> None:
        """Hold one mutable Rating value."""
        self.Rating = rating


class _Query:
    """Duck-typed ``.one()`` wrapper that raises on a missing row."""

    def __init__(self, value: _Content | None) -> None:
        """Wrap the row (or ``None`` for absent)."""
        self._value = value

    def one(self) -> _Content:
        """Return the row or raise like SQLAlchemy ``NoResultFound``."""
        if self._value is None:
            raise LookupError("not found")
        return self._value


class _BatchDB:
    """Fake Rekordbox DB that counts commits and rollbacks."""

    def __init__(self, contents: dict[str, _Content], *, drop_on_commit: str = "") -> None:
        """Seed rows; ``drop_on_commit`` names a row whose Rating resets on commit."""
        self._contents = contents
        self._drop_on_commit = drop_on_commit
        self.commits = 0
        self.rollbacks = 0

    def get_content(self, *, ID: str) -> _Query:
        """Look up one content row by ID."""
        return _Query(self._contents.get(str(ID)))

    def commit(self) -> None:
        """Count the commit and optionally simulate a lost write."""
        self.commits += 1
        if self._drop_on_commit:
            self._contents[self._drop_on_commit].Rating = 0

    def rollback(self) -> None:
        """Count a rollback."""
        self.rollbacks += 1

    def close(self) -> None:
        """No-op close."""


def _row(cid: str, rating: int) -> dict:
    """Build one accept_djay diff row that writes ``rating`` into RB ``cid``."""
    return {
        "rb_content_id": cid,
        "djay_uuid": f"u-{cid}",
        "field": "rating",
        "rb_value": "0",
        "djay_value": str(rating),
        "resolution": "accept_djay",
        "action_hint": "",
    }


@pytest.fixture
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create fake DB files and open the process gate."""
    rb_db = tmp_path / "master.db"
    rb_db.write_bytes(b"rb")
    djay_db = tmp_path / "MediaLibrary.db"
    djay_db.write_bytes(b"djay")
    monkeypatch.setattr("apps.sync.safety._is_running", lambda _name: False)
    return rb_db, djay_db


def test_several_targets_commit_once_and_all_verify(dbs, monkeypatch, capsys):
    """[if] a batch has several valid RB targets [then] one commit lands and all read back, [else stop]."""
    rb_db, djay_db = dbs
    contents = {"1": _Content(), "2": _Content(), "3": _Content()}
    db = _BatchDB(contents)
    monkeypatch.setattr("apps.shared.rekordbox_db.open_db", lambda _p: db)
    rows = [_row("1", 5), _row("2", 3), _row("3", 1)]
    assert live_run(rows, flag_ok=True, rb_db_path=rb_db, djay_db_path=djay_db) == 0
    assert db.commits == 1
    assert [contents[k].Rating for k in ("1", "2", "3")] == [5, 3, 1]
    assert "wrote rb=3 djay=0 of 3 planned" in capsys.readouterr().out


def test_absent_row_aborts_before_commit(dbs, monkeypatch):
    """[if] one requested RB content row is absent [then] abort with no commit, [else stop]."""
    rb_db, djay_db = dbs
    db = _BatchDB({"1": _Content(), "3": _Content()})
    monkeypatch.setattr("apps.shared.rekordbox_db.open_db", lambda _p: db)
    rows = [_row("1", 5), _row("2", 3), _row("3", 1)]
    with pytest.raises(SafetyAbort, match="2 not found"):
        live_run(rows, flag_ok=True, rb_db_path=rb_db, djay_db_path=djay_db)
    assert db.commits == 0
    assert db.rollbacks == 1


def test_readback_mismatch_after_commit_aborts(dbs, monkeypatch):
    """[if] post-commit readback differs from a planned rating [then] the batch aborts, [else stop]."""
    rb_db, djay_db = dbs
    db = _BatchDB({"1": _Content(), "2": _Content()}, drop_on_commit="2")
    monkeypatch.setattr("apps.shared.rekordbox_db.open_db", lambda _p: db)
    rows = [_row("1", 5), _row("2", 3)]
    with pytest.raises(SafetyAbort, match="verify_readback failed for 2"):
        live_run(rows, flag_ok=True, rb_db_path=rb_db, djay_db_path=djay_db)
    assert db.commits == 1
