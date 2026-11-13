"""SYNC-12971: the live ratings CLI fails when a planned write does not verify.

[if] a planned rating write is skipped or fails readback [then] ``live_run`` returns nonzero, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.sync.apply_ratings import live_run
from apps.sync.safety import LiveWriteSession

# Live-write mechanics against tmp fixtures only; never a real rb target.
pytestmark = [pytest.mark.requirement("SYNC-12971"), pytest.mark.rekordbox_writeback]


class _Content:
    """Duck-typed stand-in for a pyrekordbox ``DjmdContent`` row."""

    def __init__(self, rating: int = 0) -> None:
        """Hold one Rating value."""
        self.Rating = rating


class _Query:
    """Duck-typed ``.one()`` wrapper over one content row."""

    def __init__(self, value: _Content | None) -> None:
        """Wrap the row (or None for a missing row)."""
        self._value = value

    def one(self) -> _Content:
        """Return the row or raise like an empty query."""
        if self._value is None:
            raise LookupError("not found")
        return self._value


class _RBDB:
    """Minimal ``open_db`` stub; ``frozen`` keeps the Rating from changing."""

    def __init__(self, row: _Content, *, frozen: bool = False) -> None:
        """Store the single row and the frozen switch."""
        self._row = row
        self._frozen = frozen

    def get_content(self, *, ID: str) -> _Query:
        """Return the row, or a detached copy when frozen."""
        if self._frozen:
            return _Query(_Content(self._row.Rating))
        return _Query(self._row)

    def commit(self) -> None:
        """No-op commit."""

    def close(self) -> None:
        """No-op close."""


def _rows(resolution: str) -> list[dict]:
    """Return one planned rating row for the given resolution."""
    return [
        {
            "rb_content_id": "10",
            "djay_uuid": "u-10",
            "field": "rating",
            "rb_value": "4",
            "djay_value": "5",
            "resolution": resolution,
            "action_hint": "",
        }
    ]


def _dbs(tmp_path: Path) -> tuple[Path, Path]:
    """Create tmp RB and djay DB files (djay with an empty database2 table)."""
    root = tmp_path / "db"
    root.mkdir()
    rb_db = root / "master.db"
    rb_db.write_bytes(b"rb")
    djay_db = root / "MediaLibrary.db"
    with sqlite3.connect(str(djay_db)) as con:
        con.execute("CREATE TABLE database2 (collection TEXT, key TEXT, data BLOB)")
    return rb_db, djay_db


@pytest.fixture
def _gates_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open the process and iCloud gates and answer 'skip' on a verify failure."""
    monkeypatch.setattr("apps.sync.safety._is_running", lambda _name: False)
    monkeypatch.setattr("apps.sync.safety.assert_icloud_quiesced", lambda *_a, **_k: None)
    monkeypatch.setattr(LiveWriteSession, "_prompt_on_verify_failure", lambda _self, _tid: "skip")


@pytest.mark.usefixtures("_gates_open")
def test_every_planned_write_verifies_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] every planned write verifies [then] ``live_run`` returns 0 [else stop]."""
    rb_db, djay_db = _dbs(tmp_path)
    db = _RBDB(_Content(0))
    monkeypatch.setattr("apps.shared.rekordbox_db.open_db", lambda _p: db)
    rc = live_run(_rows("accept_djay"), flag_ok=True, rb_db_path=rb_db, djay_db_path=djay_db)
    assert rc == 0


@pytest.mark.usefixtures("_gates_open")
def test_djay_write_missing_row_returns_nonzero(tmp_path: Path) -> None:
    """[if] a planned djay write cannot find its row [then] ``live_run`` returns nonzero [else stop]."""
    rb_db, djay_db = _dbs(tmp_path)
    rc = live_run(_rows("accept_rb"), flag_ok=True, rb_db_path=rb_db, djay_db_path=djay_db)
    assert rc != 0


@pytest.mark.usefixtures("_gates_open")
def test_rb_readback_mismatch_returns_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a Rekordbox readback differs from the requested rating [then] ``live_run`` returns nonzero [else stop]."""
    rb_db, djay_db = _dbs(tmp_path)
    db = _RBDB(_Content(0), frozen=True)
    monkeypatch.setattr("apps.shared.rekordbox_db.open_db", lambda _p: db)
    monkeypatch.setattr("apps.sync.apply_ratings._write_rb_rating", lambda *_a: True)
    rc = live_run(_rows("accept_djay"), flag_ok=True, rb_db_path=rb_db, djay_db_path=djay_db)
    assert rc != 0
