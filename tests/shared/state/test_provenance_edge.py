"""OPEN-01c -- provenance envelope edge cases.

Complements ``test_provenance.py`` with a second file covering
additional edge paths: byte-stable JSON encoding of the ``value``
column, history ordering with interleaved sources, and the
``to_open_dj_track`` projection when only identity facts exist.
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.shared.state import provenance as prov

pytestmark = [
    pytest.mark.requirement("OPEN-01c"),
    pytest.mark.requirement("OPEN-01"),
]


TRACK_ID = "b" * 40
TS_A = "2026-03-01T09:00:00+00:00"
TS_B = "2026-03-02T09:00:00+00:00"
TS_C = "2026-03-03T09:00:00+00:00"


def _insert_track(
    conn: sqlite3.Connection,
    sid: str = TRACK_ID,
    isrc: str | None = "USRC12345678",
) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, album, "
        "isrc, duration_ms, file_path, content_hash, created_at, updated_at) "
        "VALUES (?, 'isrc', 'Edge Track', '[\"Some Artist\"]', 'Some Album', ?, 180000, "
        "'/music/edge.flac', NULL, ?, ?)",
        (sid, isrc, TS_A, TS_A),
    )


def test_write_field_canonicalises_dict_value_bytes(
    state_conn: sqlite3.Connection,
) -> None:
    """Two semantically equal dict values differing only in key order
    must collapse to the same JSON bytes, so the second write is a no-op.
    """
    _insert_track(state_conn)
    # First write: keys in one order.
    prov.write_field(
        state_conn,
        stable_id=TRACK_ID,
        field_name="beatgrid",
        value={"offset_ms": 12, "bpm": 128.0},
        source="rekordbox",
        modified_at=TS_A,
    )
    # Second write: same dict, different key insertion order -- canonical
    # JSON (sort_keys=True) makes the two byte-identical; write_field
    # must treat this as a no-op.
    changed = prov.write_field(
        state_conn,
        stable_id=TRACK_ID,
        field_name="beatgrid",
        value={"bpm": 128.0, "offset_ms": 12},
        source="rekordbox",
        modified_at=TS_A,
    )
    assert changed is False
    history_rows = state_conn.execute(
        "SELECT COUNT(*) FROM track_field_history "
        "WHERE stable_id = ? AND field_name = ?",
        (TRACK_ID, "beatgrid"),
    ).fetchone()[0]
    assert history_rows == 0


def test_read_history_orders_mixed_sources_newest_first(
    state_conn: sqlite3.Connection,
) -> None:
    _insert_track(state_conn)
    # Three writes, strictly increasing modified_at, from different sources.
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="key",
        value="8A", source="mik", modified_at=TS_A,
        now="2026-03-01T10:00:00+00:00",
    )
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="key",
        value="9A", source="rekordbox", modified_at=TS_B,
        now="2026-03-02T10:00:00+00:00",
    )
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="key",
        value="8A", source="manual", modified_at=TS_C,
        now="2026-03-03T10:00:00+00:00",
    )
    current = prov.read_field(state_conn, TRACK_ID, "key")
    assert current is not None
    assert current.value == "8A"
    assert current.source == "manual"
    history = prov.read_history(state_conn, TRACK_ID, "key")
    # History holds the two prior rows, newest first.
    assert [h.value for h in history] == ["9A", "8A"]
    assert [h.source for h in history] == ["rekordbox", "mik"]


def test_to_open_dj_track_returns_only_identity_when_no_wrapped_fields(
    state_conn: sqlite3.Connection,
) -> None:
    _insert_track(state_conn)
    doc = prov.to_open_dj_track(state_conn, TRACK_ID)
    assert doc is not None
    # Identity facts survive.
    assert doc["track_id"] == TRACK_ID
    assert doc["title"] == "Edge Track"
    assert doc["artists"] == ["Some Artist"]
    assert doc["isrc"] == "USRC12345678"
    assert doc["duration_ms"] == 180000
    # No wrapped fields written -> no provenance envelopes.
    for wrapped in ("bpm", "key", "energy", "rating"):
        assert wrapped not in doc


def test_write_field_rejects_unknown_wrapped_field(
    state_conn: sqlite3.Connection,
) -> None:
    _insert_track(state_conn)
    # ``title`` is an identity fact, not a wrapped field; write_field must
    # refuse so callers do not accidentally shadow the ``tracks`` column
    # with a provenance envelope.
    with pytest.raises(ValueError):
        prov.write_field(
            state_conn,
            stable_id=TRACK_ID,
            field_name="title",
            value="Spoofed",
            source="manual",
            modified_at=TS_A,
        )


def test_write_field_accepts_explicit_now_for_deterministic_history(
    state_conn: sqlite3.Connection,
) -> None:
    _insert_track(state_conn)
    frozen_now = "2026-03-05T12:00:00+00:00"
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="energy",
        value=0.7, source="mik", modified_at=TS_A, now=frozen_now,
    )
    prov.write_field(
        state_conn, stable_id=TRACK_ID, field_name="energy",
        value=0.8, source="mik", modified_at=TS_B, now=frozen_now,
    )
    # Both prior rows land in history under the same superseded_at clock
    # tick; the append-only schema must preserve both.
    rows = state_conn.execute(
        "SELECT superseded_at FROM track_field_history "
        "WHERE stable_id = ? AND field_name = ? "
        "ORDER BY id",
        (TRACK_ID, "energy"),
    ).fetchall()
    # Exactly one history row after two writes (first write persists in
    # track_fields; second write moves it to history).
    assert len(rows) == 1
    assert rows[0][0] == frozen_now
