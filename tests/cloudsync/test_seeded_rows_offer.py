"""Seeded rows stay off the wire until someone edits them (migrations_v26).

[if] unpresent inferred first sync runs [then] offer stays O(1) per row, [else stop].
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state.events import FakeEventBus
from apps.shared.state.library_org import update_tag_type
from apps.shared.state.writer import StateWriter
from apps.sync_hub import client, engine_changes, protocol
from apps.sync_hub.sync_set import HeldKeys, identity_row_select
from scripts.perf.seed_library_scale import seed as seed_present_library
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_track_identity_collapse import _hub_app

pytestmark = pytest.mark.requirement("CLOUDSYNC-13655")

FIRST_SYNC_WALL_S = 20.0
UNPRESENT_OFFER_COUNT = 10_000
PRESENT_SEED_COUNT = 10_000


def _offered_tag_types(path: Path) -> list[object]:
    conn = state_db.open_rw(path)
    try:
        spec = next(spec for spec in protocol.SYNC_TABLES if spec.name == "tag_type")
        return [change.pk for change in engine_changes._rows_for_table(conn, spec, HeldKeys(conn)).rows]
    finally:
        conn.close()


def _seed_unpresent_library(data_dir: Path, count: int) -> None:
    conn = state_db.open_rw(client.state_db_path(data_dir))
    try:
        state_schema.apply_migrations(conn)
        conn.executemany(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, created_at, updated_at, origin_device_id)"
            " VALUES (?, 'inferred', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'dev-a')",
            [(f"{i:040x}", f"track {i}") for i in range(count)],
        )
        conn.commit()
    finally:
        conn.close()


def _library_elections(conn: sqlite3.Connection) -> list[str]:
    election = identity_row_select(conn)
    seen: list[str] = []

    def trace(statement: str) -> None:
        if statement.strip() == election:
            seen.append(statement)

    conn.set_trace_callback(trace)
    return seen


def test_a_fresh_library_offers_no_seeded_tag_type(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    conn.close()
    assert _offered_tag_types(path) == []


def test_an_edited_seed_is_offered(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    state_schema.apply_migrations(conn)
    (tmp_path / "machine-id").write_text("a" * 32)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    update_tag_type(writer, "s", "mood", {"show_as_column": 1})
    writer.close()
    conn.close()
    assert len(_offered_tag_types(path)) == 1


def test_unpresent_first_offer_never_elects_the_whole_library(tmp_path: Path) -> None:
    """[if] 10k inferred rows lack hash and ISRC [then] offer skips library election, [else stop]."""
    spoke = tmp_path / "spoke"
    _seed_unpresent_library(spoke, UNPRESENT_OFFER_COUNT)
    conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        spec = next(s for s in protocol.SYNC_TABLES if s.name == "tracks")
        elections = _library_elections(conn)
        held = HeldKeys(conn)
        offer = engine_changes._rows_for_table(conn, spec, held)
        conn.set_trace_callback(None)
    finally:
        conn.close()
    assert len(offer.rows) == UNPRESENT_OFFER_COUNT
    assert all(change.hash_pending for change in offer.rows)
    assert elections == []


def test_unpresent_first_sync_keeps_inferred_tiers_and_skips_library_scan(
    tmp_path: Path,
) -> None:
    """[if] first run_sync offers 10k unpresent inferred tracks [then] hub keeps inferred PKs, [else stop]."""
    spoke = tmp_path / "spoke"
    hub_dir = tmp_path / "hub"
    _seed_unpresent_library(spoke, UNPRESENT_OFFER_COUNT)
    state_db.open_rw(client.state_db_path(hub_dir)).close()

    with TestClient(_hub_app(hub_dir)) as http:
        result = client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=TestClientTransport(http),
            name="spoke",
        )

    assert result.timings is not None and result.timings.kind == "first"
    assert result.hash_pending == UNPRESENT_OFFER_COUNT
    hub = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        tiers = {
            str(tier): int(count)
            for tier, count in hub.execute("SELECT stable_id_tier, COUNT(*) FROM tracks GROUP BY stable_id_tier")
        }
        path_tier = int(hub.execute("SELECT COUNT(*) FROM tracks WHERE stable_id_tier = 'path'").fetchone()[0])
    finally:
        hub.close()
    assert path_tier == 0
    assert tiers.get("inferred") == UNPRESENT_OFFER_COUNT


def test_present_seed_first_sync_stays_within_acceptable_wall(tmp_path: Path) -> None:
    """[if] in-process first sync of a 10k present seed runs [then] total_s is at most 20s, [else stop]."""
    spoke = tmp_path / "present-spoke"
    hub_dir = tmp_path / "hub"
    seed_present_library(spoke, count=PRESENT_SEED_COUNT)
    state_db.open_rw(client.state_db_path(hub_dir)).close()
    started = time.perf_counter()
    with TestClient(_hub_app(hub_dir)) as http:
        result = client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=TestClientTransport(http),
            name="present-spoke",
        )
    wall = time.perf_counter() - started
    assert result.timings is not None
    assert result.timings.kind == "first"
    assert result.timings.total_s <= FIRST_SYNC_WALL_S, (
        f"first sync total_s={result.timings.total_s:.2f} wall={wall:.2f}s"
    )
