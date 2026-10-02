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


def _rekordbox_layout(
    tmp_path: Path, fields: list[tuple[str, str]], vendor_ids: list[tuple[str, str, str | None]]
) -> Path:
    """`<data>/state/state.db` with a rekordbox mapping, and `<data>/master.plain.db`."""
    (tmp_path / "state").mkdir()
    db = _state_db(tmp_path / "state" / "state.db", fields)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE track_vendor_ids (stable_id TEXT, vendor TEXT, vendor_id TEXT, deleted_at TEXT)")
    conn.executemany("INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?, ?)", vendor_ids)
    conn.commit()
    conn.close()
    rb = sqlite3.connect(tmp_path / "master.plain.db")
    rb.execute("CREATE TABLE djmdGenre (ID TEXT, Name TEXT, rb_local_deleted INTEGER)")
    rb.execute("CREATE TABLE djmdContent (ID TEXT, GenreID TEXT, rb_local_deleted INTEGER)")
    rb.executemany("INSERT INTO djmdGenre VALUES (?, ?, 0)", [("g1", "Drum & Bass"), ("g2", "Psytrance")])
    rb.executemany("INSERT INTO djmdContent VALUES (?, ?, 0)", [("101", "g1"), ("102", "g2"), ("103", None)])
    rb.commit()
    rb.close()
    return db


def test_a_rekordbox_track_without_a_field_tag_gets_its_rekordbox_genre(tmp_path: Path) -> None:
    # The rekordbox ingest never writes track_fields.genre, so this is the
    # path every rekordbox-library track takes (0 of 337 fixtures on silver
    # had a field tag, Fri 2 Oct 2026).
    db = _rekordbox_layout(tmp_path, [], [("sid-rb", "101", None)])
    assert library_genre("sid-rb", db) == "Drum & Bass"


def test_a_field_tag_outranks_the_rekordbox_genre(tmp_path: Path) -> None:
    db = _rekordbox_layout(tmp_path, [("sid-rb", json.dumps("Psytrance"))], [("sid-rb", "101", None)])
    assert library_genre("sid-rb", db) == "Psytrance"


def test_unmapped_deleted_or_genreless_rekordbox_tracks_have_no_hint(tmp_path: Path) -> None:
    db = _rekordbox_layout(
        tmp_path,
        [],
        [("sid-gone", "102", "2026-10-01"), ("sid-none", "103", None), ("sid-orphan", "999", None)],
    )
    assert library_genre("sid-unmapped", db) is None
    assert library_genre("sid-gone", db) is None
    assert library_genre("sid-none", db) is None
    assert library_genre("sid-orphan", db) is None


def test_no_rekordbox_db_is_no_hint(tmp_path: Path) -> None:
    db = _rekordbox_layout(tmp_path, [], [("sid-rb", "101", None)])
    assert library_genre("sid-rb", db, tmp_path / "absent.db") is None
