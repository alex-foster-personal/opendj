"""Pairing writes append ``events`` rows through StateWriter (STATE-10890, #9669)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from apps.shared.pairings import PairingsRepo
from apps.shared.pairings.repo import PairingsError
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

_EDGE: dict[str, Any] = {"from_id": "a", "to_id": "b", "direction": "into"}


def _events(conn: sqlite3.Connection) -> list[tuple[str, str | None, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT kind, stable_id, payload_json FROM events WHERE kind LIKE 'pairing.%' ORDER BY id"
    ).fetchall()
    return [(kind, sid, json.loads(payload)) for kind, sid, payload in rows]


def _pairing_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM pairings").fetchone()[0]


def test_add_appends_one_pairing_add_event(state_conn, pairings_repo) -> None:
    pairings_repo.add("a", "b", direction="into", source="manual")
    assert _events(state_conn) == [("pairing.add", "a", {**_EDGE, "source": "manual"})], (
        "if PairingsRepo.add does not append one pairing.add event then broken"
    )


def test_duplicate_add_appends_second_event(state_conn, pairings_repo) -> None:
    pairings_repo.add("a", "b", direction="into", source="manual")
    pairings_repo.add("a", "b", direction="into", source="learned")
    events = _events(state_conn)
    assert [e[0] for e in events] == ["pairing.add", "pairing.add"], (
        "if a duplicate add does not append its own pairing.add event then broken"
    )
    assert events[1][2]["source"] == "learned"
    assert _pairing_count(state_conn) == 1


def test_remove_appends_pairing_remove_event(state_conn, pairings_repo) -> None:
    pairings_repo.add("a", "b", direction="into", source="learned")
    assert pairings_repo.remove("a", "b", "into") is True
    assert _events(state_conn)[-1] == ("pairing.remove", "a", {**_EDGE, "source": "learned"}), (
        "if PairingsRepo.remove does not append pairing.remove with the row's source then broken"
    )
    assert not pairings_repo.exists("a", "b", "into")


def test_remove_of_missing_edge_appends_nothing(state_conn, pairings_repo) -> None:
    assert pairings_repo.remove("a", "b", "into") is False
    assert _events(state_conn) == [], "if a remove that matches nothing appends an event then broken"


def test_add_sequence_add_dup_remove(state_conn, pairings_repo) -> None:
    pairings_repo.add("a", "b", direction="into")
    pairings_repo.add("a", "b", direction="into", notes="again")
    pairings_repo.remove("a", "b", "into")
    assert [e[0] for e in _events(state_conn)] == [
        "pairing.add",
        "pairing.add",
        "pairing.remove",
    ], "if add, duplicate add, remove do not log three pairing events then broken"


def test_failed_event_insert_rolls_back_row(state_conn, pairings_repo) -> None:
    state_conn.execute("DROP TABLE events")
    with pytest.raises(sqlite3.OperationalError):
        pairings_repo.add("a", "b", direction="into")
    assert _pairing_count(state_conn) == 0, "if a failed event insert leaves the pairing row then broken"


def test_outer_writer_unit_rollback_drops_row_and_event(state_conn) -> None:
    bus = FakeEventBus()
    with (
        StateWriter(state_conn, bus=bus) as writer,
        pytest.raises(RuntimeError, match="boom"),
        writer.transaction(),
    ):
        PairingsRepo(state_conn, ensure_schema=False, writer=writer).add("a", "b")
        raise RuntimeError("boom")
    assert _pairing_count(state_conn) == 0
    assert _events(state_conn) == []
    assert bus.events == [], "if a rolled-back outer unit keeps the pairing row or its event then broken"


def test_injected_writer_publishes_after_commit(state_conn) -> None:
    bus = FakeEventBus()
    with StateWriter(state_conn, bus=bus) as writer:
        PairingsRepo(state_conn, ensure_schema=False, writer=writer).add("a", "b")
    assert [e.kind for e in bus.events] == ["pairing.add"]


def test_writer_bound_to_other_conn_is_refused(state_conn, tmp_path) -> None:
    other = sqlite3.connect(str(tmp_path / "state.db"), isolation_level=None)
    try:
        with StateWriter(other, bus=FakeEventBus()) as writer, pytest.raises(PairingsError):
            PairingsRepo(state_conn, ensure_schema=False, writer=writer)
    finally:
        other.close()


def test_validation_error_appends_no_event(state_conn, pairings_repo) -> None:
    with pytest.raises(PairingsError):
        pairings_repo.add("a", "a")
    assert _events(state_conn) == []


def test_reads_need_no_writer(state_conn, pairings_repo) -> None:
    pairings_repo.add("a", "b", direction="into")
    reader = PairingsRepo(state_conn, ensure_schema=False)
    assert reader.exists("a", "b", "into")
    assert [e.to_stable_id for e in reader.get_neighbors("a")] == ["b"]
    assert [e.from_stable_id for e in reader.list_all()] == ["a"]
