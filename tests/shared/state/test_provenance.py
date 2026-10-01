"""OPEN-01c provenance envelope writer + reader tests."""
from __future__ import annotations

import sqlite3

import pytest

from apps.shared.state import provenance as prov
from apps.shared.state.types import ProvenanceValue

pytestmark = [
    pytest.mark.requirement("OPEN-01c"),
    pytest.mark.requirement("OPEN-01"),
]

TRACK_ID = "a" * 40
TS = "2026-02-02T09:00:00+00:00"
TS2 = "2026-02-03T09:00:00+00:00"


def _insert_track(conn: sqlite3.Connection, sid: str = TRACK_ID, isrc: str | None = "GBCEN0900132") -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, album, "
        "isrc, duration_ms, file_path, content_hash, created_at, updated_at) "
        "VALUES (?, 'isrc', 'Lanterns', '[\"Marlow Quay\"]', 'PHRB', ?, 634000, "
        "'/x.flac', NULL, ?, ?)",
        (sid, isrc, TS, TS),
    )


def test_write_field_inserts_row(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    changed = prov.write_field(
        state_conn,
        stable_id=TRACK_ID,
        field_name="bpm",
        value=128.0,
        source="rekordbox",
        modified_at=TS,
    )
    assert changed is True
    pv = prov.read_field(state_conn, TRACK_ID, "bpm")
    assert pv is not None
    assert pv.value == 128.0
    assert pv.source == "rekordbox"
    assert pv.modified_at == TS


def test_write_field_overwrite_appends_history(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="bpm", value=128.0,
        source="rekordbox", modified_at=TS,
    )
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="bpm", value=130.0,
        source="mik", modified_at=TS2,
    )
    pv = prov.read_field(state_conn, TRACK_ID, "bpm")
    assert pv.value == 130.0
    assert pv.source == "mik"
    history = prov.read_history(state_conn, TRACK_ID, "bpm")
    assert len(history) == 1
    assert history[0].value == 128.0
    assert history[0].source == "rekordbox"


def test_write_field_byte_equal_repeat_is_no_op(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="bpm", value=128.0,
        source="rekordbox", modified_at=TS,
    )
    # events before + after: must be exactly one.
    before = state_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    changed = prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="bpm", value=128.0,
        source="rekordbox", modified_at=TS,
    )
    after = state_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert changed is False
    assert before == after
    # Exactly one row in track_field_history? Actually zero.
    h_count = state_conn.execute(
        "SELECT COUNT(*) FROM track_field_history"
    ).fetchone()[0]
    assert h_count == 0


def test_write_field_rejects_unknown_field_name(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    with pytest.raises(ValueError):
        prov.write_field(
            state_conn, stable_id=TRACK_ID, field_name="not_a_field",
            value=1, source="rekordbox", modified_at=TS,
        )


def test_write_field_rejects_invalid_source(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    with pytest.raises(ValueError):
        prov.write_field(
            state_conn, stable_id=TRACK_ID, field_name="bpm",
            value=128, source="HAL9000", modified_at=TS,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "bad", ["", "2026-02-02T09:00:00", "2026-02-02 09:00:00Z", "not-a-date", "2026-02-02T09:00:00-05:00"],
)
def test_write_field_rejects_non_rfc3339_modified_at(
    state_conn: sqlite3.Connection, bad: str
) -> None:
    _insert_track(state_conn)
    with pytest.raises(ValueError):
        prov.write_field(
            state_conn, stable_id=TRACK_ID, field_name="bpm",
            value=128, source="rekordbox", modified_at=bad,
        )


def test_write_field_rejects_out_of_range_confidence(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    with pytest.raises(ValueError):
        prov.write_field(
            state_conn, stable_id=TRACK_ID, field_name="bpm",
            value=128, source="rekordbox", modified_at=TS, confidence=1.5,
        )


def test_read_field_returns_none_when_missing(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    assert prov.read_field(state_conn, TRACK_ID, "bpm") is None


def test_read_history_newest_first(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    prov.write_field(state_conn, stable_id=TRACK_ID, field_name="bpm",
                     value=128, source="rekordbox", modified_at=TS)
    prov.write_field(state_conn, stable_id=TRACK_ID, field_name="bpm",
                     value=129, source="rekordbox", modified_at="2026-02-03T09:00:00Z",
                     now="2026-02-03T10:00:00+00:00")
    prov.write_field(state_conn, stable_id=TRACK_ID, field_name="bpm",
                     value=130, source="rekordbox", modified_at="2026-02-04T09:00:00Z",
                     now="2026-02-04T10:00:00+00:00")
    history = prov.read_history(state_conn, TRACK_ID, "bpm")
    assert [h.value for h in history] == [129, 128]


def test_history_append_only_on_same_clock_tick(
    state_conn: sqlite3.Connection,
) -> None:
    """[I1] Two overwrites sharing the same frozen ``now`` must both land.

    The v1 PK ``(stable_id, field_name, superseded_at)`` would silently
    overwrite the first history row. Schema v2 adds an autoincrement id so
    the table is truly append-only; both rows must survive.
    """
    _insert_track(state_conn)
    frozen = "2026-02-04T10:00:00+00:00"
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="bpm",
        value=128, source="rekordbox", modified_at=TS, now=frozen,
    )
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="bpm",
        value=129, source="rekordbox", modified_at="2026-02-03T09:00:00Z",
        now=frozen,
    )
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="bpm",
        value=130, source="rekordbox", modified_at="2026-02-04T09:00:00Z",
        now=frozen,
    )
    count = state_conn.execute(
        "SELECT COUNT(*) FROM track_field_history "
        "WHERE stable_id = ? AND field_name = ?",
        (TRACK_ID, "bpm"),
    ).fetchone()[0]
    assert count == 2, "both prior rows must be preserved in history"
    history = prov.read_history(state_conn, TRACK_ID, "bpm")
    assert [h.value for h in history] == [129, 128]


def test_to_open_dj_track_matches_schema(state_conn: sqlite3.Connection) -> None:
    _insert_track(state_conn)
    state_conn.execute(
        "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id) VALUES (?, ?, ?)",
        (TRACK_ID, "rekordbox", "42"),
    )
    prov.write_field(state_conn, stable_id=TRACK_ID, field_name="bpm",
                     value=128.0, source="rekordbox", modified_at=TS)
    prov.write_field(state_conn, stable_id=TRACK_ID, field_name="key",
                     value="8A", source="mik", modified_at=TS, confidence=0.9)
    doc = prov.to_open_dj_track(state_conn, TRACK_ID)
    assert doc is not None
    # Identity facts unwrapped.
    assert doc["track_id"] == TRACK_ID
    assert doc["title"] == "Lanterns"
    assert doc["artists"] == ["Marlow Quay"]
    assert doc["isrc"] == "GBCEN0900132"
    assert doc["duration_ms"] == 634000
    # Provenance envelopes.
    assert doc["bpm"]["value"] == 128.0
    assert doc["bpm"]["source"] == "rekordbox"
    assert "modified_at" in doc["bpm"]
    assert doc["key"]["confidence"] == 0.9
    # Vendor ids map.
    assert doc["vendor_ids"] == {"rekordbox": "42"}


def test_to_open_dj_track_missing_returns_none(state_conn: sqlite3.Connection) -> None:
    assert prov.to_open_dj_track(state_conn, "z" * 40) is None


def test_provenance_value_as_open_dj_omits_none_confidence() -> None:
    pv = ProvenanceValue(value=1, source="rekordbox", modified_at=TS)
    d = pv.as_open_dj()
    assert "confidence" not in d
    assert d["source"] == "rekordbox"
    assert d["value"] == 1


def test_provenance_value_as_open_dj_includes_confidence() -> None:
    pv = ProvenanceValue(value=1, source="mik", modified_at=TS, confidence=0.75)
    d = pv.as_open_dj()
    assert d["confidence"] == 0.75


def test_sources_mirror_schema_check_constraint() -> None:
    from apps.shared.state.types import SOURCES
    # If we ever change the schema CHECK list, this test must be updated too.
    assert SOURCES == frozenset(
        {"mik", "rekordbox", "djay", "serato", "traktor", "open-dj-tool",
         "manual", "inferred", "webui"}
    )
