"""The backfill backend's genre hint: state.db `track_fields.genre`, or nothing.

A hint that is absent must read as absent (None), never as a guessed genre;
a present tag must reach the octave policy verbatim.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.analysis.backends.genre_hint import GENRE_HINT_ENV, library_genre


def _state_db(path: Path, rows: list[tuple[str, str]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE track_fields (stable_id TEXT, field_name TEXT, value_json TEXT, "
        "source TEXT, confidence REAL, modified_at TEXT, PRIMARY KEY (stable_id, field_name))"
    )
    conn.executemany(
        "INSERT INTO track_fields VALUES (?, 'genre', ?, 'rekordbox', NULL, '2026-09-29')",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def test_a_tagged_track_returns_its_tag(tmp_path: Path) -> None:
    db = _state_db(tmp_path / "state.db", [("sid-dnb", json.dumps("Drum & Bass"))])
    assert library_genre("sid-dnb", db) == "Drum & Bass"


def test_an_untagged_or_blank_track_returns_none(tmp_path: Path) -> None:
    db = _state_db(tmp_path / "state.db", [("sid-blank", json.dumps("   "))])
    assert library_genre("sid-blank", db) is None
    assert library_genre("sid-missing", db) is None


def test_no_state_db_is_no_hint(tmp_path: Path) -> None:
    assert library_genre("sid", tmp_path / "absent.db") is None


def test_the_hint_can_be_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _state_db(tmp_path / "state.db", [("sid-dnb", json.dumps("dnb"))])
    monkeypatch.setenv(GENRE_HINT_ENV, "off")
    assert library_genre("sid-dnb", db) is None
