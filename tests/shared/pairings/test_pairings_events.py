"""[if] PairingsRepo mutates pairings [then] each write appends a durable pairing event, [else stop]."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from apps.shared.pairings import PairingsRepo, ensure_phase08_tables
from apps.shared.pairings.repo import PairingsError
from apps.shared.state.db import open_rw
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("STATE-12739")

_EDGE: dict[str, Any] = {"from_id": "a", "to_id": "b", "direction": "into"}


def _events(conn: sqlite3.Connection) -> list[tuple[str, str | None, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT kind, stable_id, payload_json FROM events WHERE kind LIKE 'pairing.%' ORDER BY id"
    ).fetchall()
    return [(kind, sid, json.loads(payload)) for kind, sid, payload in rows]


def _pairing_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM pairings").fetchone()[0]


def test_add_on_open_rw_appends_pairing_add_event(tmp_path: Path) -> None:
    """[if] PairingsRepo upserts on open_rw state.db [then] one pairing.add event, [else stop]."""
    conn = open_rw(tmp_path / "state.db")
    try:
        ensure_phase08_tables(conn)
        PairingsRepo(conn, ensure_schema=False).add("a", "b", direction="into", source="manual")
        assert _events(conn) == [("pairing.add", "a", {**_EDGE, "source": "manual"})]
    finally:
        conn.close()


def test_add_appends_one_pairing_add_event(state_conn, pairings_repo) -> None:
    """[if] PairingsRepo.add upserts an edge [then] exactly one pairing.add event, [else stop]."""
    pairings_repo.add("a", "b", direction="into", source="manual")
    assert _events(state_conn) == [("pairing.add", "a", {**_EDGE, "source": "manual"})]


def test_add_routes_through_writer_upsert(state_conn) -> None:
    """[if] PairingsRepo.add runs [then] StateWriter.upsert_pairing is used, [else stop]."""
    bus = FakeEventBus()
    with StateWriter(state_conn, bus=bus) as writer:
        repo = PairingsRepo(state_conn, ensure_schema=False, writer=writer)
        with patch.object(writer, "upsert_pairing", wraps=writer.upsert_pairing) as spy:
            repo.add("a", "b", direction="into")
            spy.assert_called_once()


def test_duplicate_add_appends_second_event(state_conn, pairings_repo) -> None:
    """[if] the same edge is upserted twice [then] two pairing.add events exist, [else stop]."""
    pairings_repo.add("a", "b", direction="into", source="manual")
    pairings_repo.add("a", "b", direction="into", source="learned")
    events = _events(state_conn)
    assert [e[0] for e in events] == ["pairing.add", "pairing.add"]
    assert events[1][2]["source"] == "learned"
    assert _pairing_count(state_conn) == 1


def test_remove_appends_pairing_remove_event(state_conn, pairings_repo) -> None:
    """[if] PairingsRepo.remove deletes an edge [then] pairing.remove is in events, [else stop]."""
    pairings_repo.add("a", "b", direction="into", source="learned")
    assert pairings_repo.remove("a", "b", "into") is True
    assert _events(state_conn)[-1] == ("pairing.remove", "a", {**_EDGE, "source": "learned"})
    assert not pairings_repo.exists("a", "b", "into")


def test_remove_of_missing_edge_appends_nothing(state_conn, pairings_repo) -> None:
    """[if] remove matches no edge [then] no pairing.remove event, [else stop]."""
    assert pairings_repo.remove("a", "b", "into") is False
    assert _events(state_conn) == []


def test_add_sequence_add_dup_remove(state_conn, pairings_repo) -> None:
    """[if] add, duplicate add, remove run [then] three pairing events in order, [else stop]."""
    pairings_repo.add("a", "b", direction="into")
    pairings_repo.add("a", "b", direction="into", notes="again")
    pairings_repo.remove("a", "b", "into")
    assert [e[0] for e in _events(state_conn)] == [
        "pairing.add",
        "pairing.add",
        "pairing.remove",
    ]


def test_failed_event_insert_rolls_back_row(state_conn, pairings_repo) -> None:
    """[if] the event insert fails [then] the pairing row rolls back, [else stop]."""
    state_conn.execute("DROP TABLE events")
    with pytest.raises(sqlite3.OperationalError):
        pairings_repo.add("a", "b", direction="into")
    assert _pairing_count(state_conn) == 0


def test_outer_writer_unit_rollback_drops_row_and_event(state_conn) -> None:
    """[if] pairing write is in an outer write unit [then] rollback drops row and event, [else stop]."""
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
    assert bus.events == []


def test_injected_writer_publishes_after_commit(state_conn) -> None:
    """[if] add uses an injected writer [then] bus publish lands after commit, [else stop]."""
    bus = FakeEventBus()
    with StateWriter(state_conn, bus=bus) as writer:
        PairingsRepo(state_conn, ensure_schema=False, writer=writer).add("a", "b")
    assert [e.kind for e in bus.events] == ["pairing.add"]


def test_writer_bound_to_other_conn_is_refused(state_conn, tmp_path) -> None:
    """[if] writer is bound to another connection [then] PairingsRepo refuses, [else stop]."""
    other = sqlite3.connect(str(tmp_path / "state.db"), isolation_level=None)
    try:
        with StateWriter(other, bus=FakeEventBus()) as writer, pytest.raises(PairingsError):
            PairingsRepo(state_conn, ensure_schema=False, writer=writer)
    finally:
        other.close()


def test_validation_error_appends_no_event(state_conn, pairings_repo) -> None:
    """[if] add validation fails [then] no pairing event is appended, [else stop]."""
    with pytest.raises(PairingsError):
        pairings_repo.add("a", "a")
    assert _events(state_conn) == []


def test_reads_need_no_writer(state_conn, pairings_repo) -> None:
    """[if] pairings reads run [then] no writer is required on the repo, [else stop]."""
    pairings_repo.add("a", "b", direction="into")
    reader = PairingsRepo(state_conn, ensure_schema=False)
    assert reader.exists("a", "b", "into")
    assert [e.to_stable_id for e in reader.get_neighbors("a")] == ["b"]
    assert [e.from_stable_id for e in reader.list_all()] == ["a"]
