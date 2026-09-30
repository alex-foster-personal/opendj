"""Issue #4396: a no-op sync must not re-hash every table, and must still see every change.

The gate (:mod:`apps.sync_hub.digest_gate`) reuses a digest only when the
changelog seqs, every `sqlite_master` definition (rollback-safe, not
`PRAGMA schema_version`), the identity remap and every digested table's
trigger-maintained write token are all unchanged. These tests prove both
halves against the real hub router and real state DBs:

* PRESENCE of the saving: a settled no-op sync walks zero digests and is
  served from the gate on both sides, counted by the gate's own production
  counters (:func:`apps.sync_hub.digest_gate.stats`), not by a replaced
  function.
* PRESENCE of detection (the overshoot control): a row changed BEHIND the
  changelog, a rolled-back write, a schema change and a remap change each
  still reach the digest, compared against an uncached walk.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import migrations_v21
from apps.shared.state.machine_identity import IS_HUB_ENV
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
def _no_hub_env() -> None:
    """``MDT_IS_HUB`` must be unset by the caller; a shell that sets it fails loud.

    Established outside the test process rather than mutated in it, so the
    DBs these tests open take the spoke path because that is the environment.
    """
    if IS_HUB_ENV in os.environ:
        pytest.fail(f"unset {IS_HUB_ENV} before running this suite: it steers migrations")


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


class Since:
    """Gate counters moved since construction, hub and spoke alike (one process)."""

    def __init__(self) -> None:
        self._start = digest_gate.stats()

    @property
    def walks(self) -> int:
        return digest_gate.stats().walks - self._start.walks

    @property
    def hits(self) -> int:
        return digest_gate.stats().hits - self._start.hits


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


def test_noop_sync_after_a_settled_sync_walks_no_digest(
    spoke_a: Path, hub: _TestClientTransport
) -> None:
    first = Since()
    _settled_pair(spoke_a, hub)
    assert first.walks >= 2, "positive control: the first sync must walk both sides"

    since = Since()
    noop = _sync(spoke_a, hub, "spoke-a")

    assert noop.timings is not None and noop.timings.kind == "noop"
    assert (noop.pushed, noop.pulled, noop.rounds) == (0, 0, 1)
    assert since.walks == 0, f"a no-op sync re-walked {since.walks} digest(s)"
    assert since.hits >= 2, "both sides must be served by the gate, not skipped"


def test_changelog_logged_edit_rewalks_and_still_converges(
    spoke_a: Path, hub: _TestClientTransport
) -> None:
    _settled_pair(spoke_a, hub)
    conn = _open(spoke_a)
    try:
        _set_track_title(conn, "trk-gate-1", title="edited", updated_at=_T1, origin=_DEV_A)
    finally:
        conn.close()
    since = Since()

    result = _sync(spoke_a, hub, "spoke-a")

    assert result.pushed == 1
    assert since.walks >= 2, "an edit must re-walk the digest on both sides"


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


def test_rolled_back_schema_change_never_lends_its_digest_to_a_later_one(
    spoke_a: Path,
) -> None:
    """``PRAGMA schema_version`` rolls back, so two schemas can share one cookie."""
    _seed_common_track((spoke_a,), "trk-gate-1")
    conn = _open(spoke_a)
    try:
        _digest_is_honest(conn)
        cookie_before = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE track_fields ADD COLUMN first_probe TEXT")
        protocol.sync_digest(conn)  # cached under the uncommitted schema
        conn.execute("ROLLBACK")
        conn.execute("ALTER TABLE track_fields ADD COLUMN second_probe INTEGER")
        cookie_after = conn.execute("PRAGMA schema_version").fetchone()[0]
        assert cookie_after == cookie_before + 1, (
            "precondition: the committed ALTER reuses the rolled-back cookie"
        )

        _digest_is_honest(conn)
    finally:
        conn.close()


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


def test_missing_trigger_disables_the_gate(spoke_a: Path) -> None:
    """A table the gate cannot watch is always walked, never trusted."""
    _seed_common_track((spoke_a,), "trk-gate-1")
    conn = _open(spoke_a)
    try:
        protocol.sync_digest(conn)
        intact = Since()
        protocol.sync_digest(conn)
        assert (intact.walks, intact.hits) == (0, 1), (
            "positive control: an intact gate must serve the second digest"
        )
        conn.execute(f"DROP TRIGGER {migrations_v21.trigger_name('track_fields', 'UPDATE')}")
        broken = Since()
        protocol.sync_digest(conn)
        protocol.sync_digest(conn)
    finally:
        conn.close()
    assert (broken.walks, broken.hits) == (2, 0)


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
    # sqlite_master is itself part of the gate key (every definition, verbatim).
    watched = set(migrations_v21.WRITE_TOKEN_TABLES) | {REMAP_TABLE, "sqlite_master"}
    assert read <= watched, f"digest reads unwatched table(s): {sorted(read - watched)}"


def test_write_token_tables_are_exactly_the_digest_set() -> None:
    assert set(migrations_v21.WRITE_TOKEN_TABLES) == set(sync_set.FK_ORDER)
    assert digest_gate.CFG.IDENTITY_REMAP_TABLE == REMAP_TABLE
