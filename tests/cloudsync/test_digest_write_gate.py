"""Issue #4396: a no-op sync must not re-hash every table, and must still see every change.

The gate (:mod:`apps.sync_hub.digest_gate`) reuses a digest only when the
changelog seqs, the schema cookie, the identity remap and every digested
table's trigger-maintained write token are all unchanged. These tests prove
both halves against the real hub router and real state DBs:

* PRESENCE of the saving: a settled no-op sync walks zero tables, on either
  side, counted on the real ``table_digest`` (a pass-through spy, so every
  walk it counts really ran).
* PRESENCE of detection (the overshoot control): a row changed BEHIND the
  changelog, a rolled-back write, a schema change and a remap change each
  still reach the digest, compared against an uncached walk.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import migrations_v20
from apps.sync_hub import client, digest_gate, engine, protocol, service, sync_set
from apps.sync_hub.engine_identity_map import REMAP_TABLE, ensure_identity_remap_table
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T1,
    _open,
    _seed_common_track,
    _set_track_title,
    _sync,
    _TestClientTransport,
)

# ----- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    return tmp_path / "spoke-a"


@pytest.fixture
def hub(tmp_path: Path) -> Iterator[_TestClientTransport]:
    """The real sync router on an empty hub DB. See ``test_hub_sync.hub``."""
    hub_dir = tmp_path / "hub"
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every table the REAL ``table_digest`` walked, hub and spoke alike."""
    seen: list[str] = []
    real = protocol.table_digest

    def counting(conn: sqlite3.Connection, table: str, *args: Any) -> Any:
        seen.append(table)
        return real(conn, table, *args)

    monkeypatch.setattr(protocol, "table_digest", counting)
    return seen


# ----- helpers ----------------------------------------------------------------


def _settled_pair(spoke: Path, hub: _TestClientTransport) -> None:
    """One track, synced to the hub and back, so both sides agree."""
    _seed_common_track((spoke,), "trk-gate-1")
    _sync(spoke, hub, "spoke-a")


def _digest_is_honest(conn: sqlite3.Connection) -> protocol.SyncDigest:
    """``sync_digest`` after asserting it equals a walk that bypasses the gate."""
    gated = protocol.sync_digest(conn)
    assert gated == protocol.compute_sync_digest(conn), (
        "the gate returned a digest that differs from an uncached walk of the same DB"
    )
    return gated


# ----- the saving -------------------------------------------------------------


def test_noop_sync_after_a_settled_sync_walks_no_table(
    spoke_a: Path, hub: _TestClientTransport, walks: list[str]
) -> None:
    _settled_pair(spoke_a, hub)
    assert walks, "positive control: the first sync must walk tables through the spy"
    walks.clear()

    noop = _sync(spoke_a, hub, "spoke-a")

    assert noop.timings is not None and noop.timings.kind == "noop"
    assert (noop.pushed, noop.pulled, noop.rounds) == (0, 0, 1)
    assert walks == [], f"a no-op sync re-walked {len(walks)} table(s): {sorted(set(walks))}"


def test_changelog_logged_edit_rewalks_and_still_converges(
    spoke_a: Path, hub: _TestClientTransport, walks: list[str]
) -> None:
    _settled_pair(spoke_a, hub)
    conn = _open(spoke_a)
    try:
        _set_track_title(conn, "trk-gate-1", title="edited", updated_at=_T1, origin=_DEV_A)
    finally:
        conn.close()
    walks.clear()

    result = _sync(spoke_a, hub, "spoke-a")

    assert result.pushed == 1
    assert set(sync_set.FK_ORDER) <= set(walks), "an edit must re-walk every table, both sides"


# ----- the overshoot controls ---------------------------------------------------


def test_row_changed_behind_the_changelog_still_raises_digest_mismatch(
    spoke_a: Path, hub: _TestClientTransport
) -> None:
    """The issue's own acceptance test: divergence the changelog never saw."""
    _settled_pair(spoke_a, hub)
    _sync(spoke_a, hub, "spoke-a")  # warm both caches on the settled state
    conn = _open(spoke_a)
    try:
        seq_before = engine.local_seq(conn)
        changed = conn.execute(
            "UPDATE tracks SET title = 'changed behind the changelog' WHERE stable_id = ?",
            ("trk-gate-1",),
        ).rowcount
        assert changed == 1
        assert engine.local_seq(conn) == seq_before, "the write must bypass the changelog"
    finally:
        conn.close()

    with pytest.raises(client.SyncDigestMismatch):
        _sync(spoke_a, hub, "spoke-a")


def test_rolled_back_write_never_lends_its_digest_to_a_later_write(
    spoke_a: Path,
) -> None:
    """A counter would reuse a rolled-back generation; a random token cannot."""
    _seed_common_track((spoke_a,), "trk-gate-1")
    conn = _open(spoke_a)
    try:
        _digest_is_honest(conn)
        conn.execute("BEGIN")
        conn.execute("UPDATE tracks SET title = 'rolled back' WHERE stable_id = 'trk-gate-1'")
        protocol.sync_digest(conn)  # cached under the uncommitted state
        conn.execute("ROLLBACK")
        conn.execute("UPDATE tracks SET title = 'committed' WHERE stable_id = 'trk-gate-1'")

        _digest_is_honest(conn)
    finally:
        conn.close()


def test_schema_change_reaches_the_digest(spoke_a: Path) -> None:
    _seed_common_track((spoke_a,), "trk-gate-1")
    conn = _open(spoke_a)
    try:
        before = _digest_is_honest(conn)
        conn.execute("ALTER TABLE track_fields ADD COLUMN gate_probe TEXT")
        after = _digest_is_honest(conn)
    finally:
        conn.close()
    assert after.tables["track_fields"] != before.tables["track_fields"]


def test_identity_remap_change_reaches_the_digest(spoke_a: Path) -> None:
    _seed_common_track((spoke_a,), "trk-gate-1")
    _seed_common_track((spoke_a,), "trk-gate-2")
    conn = _open(spoke_a)
    try:
        ensure_identity_remap_table(conn)
        before = _digest_is_honest(conn)
        conn.execute(
            f"INSERT INTO {REMAP_TABLE}(loser_pk, survivor_pk) VALUES (?, ?)",
            ("trk-gate-2", "trk-gate-1"),
        )
        after = _digest_is_honest(conn)
    finally:
        conn.close()
    assert after.tables["tracks"] != before.tables["tracks"]


def test_missing_trigger_disables_the_gate(spoke_a: Path, walks: list[str]) -> None:
    """A table the gate cannot watch is always walked, never trusted."""
    _seed_common_track((spoke_a,), "trk-gate-1")
    conn = _open(spoke_a)
    try:
        protocol.sync_digest(conn)
        walks.clear()
        protocol.sync_digest(conn)
        assert walks == [], "positive control: an intact gate must skip the second walk"
        conn.execute(f"DROP TRIGGER {migrations_v20.trigger_name('track_fields', 'UPDATE')}")
        protocol.sync_digest(conn)
        protocol.sync_digest(conn)
    finally:
        conn.close()
    assert walks.count("tracks") == 2


# ----- the invariant the gate rests on ------------------------------------------


def test_gate_watches_every_table_the_digest_reads(spoke_a: Path) -> None:
    """Every table a digest walk READS is one the gate watches.

    Read through SQLite's own authorizer, so a future dependency on a new
    table fails here instead of being served from a stale cache.
    """
    _seed_common_track((spoke_a,), "trk-gate-1")
    conn = _open(spoke_a)
    ensure_identity_remap_table(conn)
    read: set[str] = set()

    def authorizer(action: int, arg1: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_READ and arg1 is not None:
            read.add(arg1)
        return sqlite3.SQLITE_OK

    try:
        conn.set_authorizer(authorizer)
        protocol.compute_sync_digest(conn)
        conn.set_authorizer(None)
    finally:
        conn.close()
    assert "tracks" in read, "positive control: the authorizer must see the walk"
    # sqlite_master changes only by DDL, which moves PRAGMA schema_version.
    watched = set(migrations_v20.WRITE_TOKEN_TABLES) | {REMAP_TABLE, "sqlite_master"}
    assert read <= watched, f"digest reads unwatched table(s): {sorted(read - watched)}"


def test_write_token_tables_are_exactly_the_digest_set() -> None:
    assert set(migrations_v20.WRITE_TOKEN_TABLES) == set(sync_set.FK_ORDER)
    assert digest_gate.CFG.IDENTITY_REMAP_TABLE == REMAP_TABLE
