"""Round 3 hardening: the round 2 adversarial findings, N4/A4/N5/4a.

Companion to :mod:`tests.cloudsync.test_hub_sync_hardening`, which holds the
round 1 findings. Split because one file holding both crossed the "too long
to hold in your head" line, and because the two rounds fail for different
reasons: round 1 was "the merge is wrong", round 2 is "the merge is right
and the machine stops anyway".

Round 4 quality-gate ratchet split this file a second time: the digest-fence
findings (6b/N7) and the generation-token/retention findings (N6) now live in
:mod:`tests.cloudsync.test_hub_sync_round3_settling`, which imports nothing
back from here. This module keeps N4/A4, N5 and 4a's hub blast radius.

Every test here is one reproduction sketch from
``.planning/cloudsync-round2-adversarial.md``. The mapping, so a failure
names the defect it just let back in:

- N4 / A4  -> ``sync_policies``, ``playlist_pins`` and ``track_locations``
  all point at ``machines(machine_id)``, and every spoke holds its peers'
  rows by design, so a hub that had not met one of those machines refused
  the whole push with a FOREIGN KEY 409. ADR 08 point 4's restore recovery
  is the push most likely to carry one, so the recovery triggered the wedge.
- N5       -> natural-key resolution took the FIRST applicable partial
  UNIQUE index, leaving the other to fail as an unresolvable 409.
- 4a (hub) -> a ``hub_changelog`` entry naming a hard-deleted row answered
  EVERY spoke's pull with a 409, forever, with no repair path.

Acceptance criteria, one test each:
- if a push carrying a row for a machine the hub has not met is refused, the
  fleet snapshot is not travelling with the push -- broken.
- if a collision on the SECOND partial UNIQUE index 409s instead of
  resolving, natural-key resolution still picks one index and hopes --
  broken.
- if a hard-deleted hub row makes every spoke's pull 409 forever, one manual
  DELETE still takes the fleet offline -- broken.

6b/N7 and N6's acceptance criteria live in
``test_hub_sync_round3_settling.py``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import locations, sync_stamp
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, engine, protocol, service
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T0,
    _T1,
    _T2,
    _T3,
    _insert_track,
    _log,
    _open,
    _seed_common_track,
    _sync,
    _TestClientTransport,
    _track_title,
)
from tests.cloudsync.test_hub_sync_hardening import _changelog_pks, _locations

pytestmark = pytest.mark.requirement("CAT-04")


# ----- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    return tmp_path / "spoke-a"


@pytest.fixture
def spoke_b(tmp_path: Path) -> Path:
    return tmp_path / "spoke-b"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


def _insert_machine(
    conn: sqlite3.Connection, machine_id: str, *, name: str, platform: str = "linux"
) -> None:
    """A ``machines`` row for a peer this machine knows about."""
    conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, data_root, "
        "first_seen, last_seen) VALUES (?, ?, ?, 0, NULL, ?, ?)",
        (machine_id, name, platform, _T0, _T0),
    )


def _insert_policy(
    conn: sqlite3.Connection,
    *,
    machine_id: str,
    asset_kind: str,
    mode: str,
    origin: str,
    updated_at: str,
) -> None:
    stamped = _log(conn, "sync_policies", (machine_id, asset_kind), origin, updated_at)
    conn.execute(
        "INSERT INTO sync_policies(machine_id, asset_kind, mode, updated_at, "
        "origin_device_id) VALUES (?, ?, ?, ?, ?)",
        (machine_id, asset_kind, mode, stamped, origin),
    )


# ----- N4 / A4: the pusher's machines snapshot ------------------------------


def test_a_policy_row_for_a_machine_the_hub_has_not_met_is_accepted(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """A4's sketch: a row whose ``machine_id`` the hub has never seen.

    Round 2 observed ``a4 HTTP 409 ... FOREIGN KEY constraint failed`` on
    every retry: ``sync_policies.machine_id`` REFERENCES ``machines``, and
    ``push`` merged only the caller's own row (at hello), never the fleet it
    knows. The push carries the snapshot now, so the parent row lands first.
    """
    conn_a = _open(spoke_a)
    try:
        machine_a = sync_stamp.ensure_local_machine(conn_a)
        _insert_machine(conn_a, "peer-x" + "0" * 26, name="peer-x")
        _insert_policy(
            conn_a,
            machine_id="peer-x" + "0" * 26,
            asset_kind="audio",
            mode="pinned",
            origin=machine_a,
            updated_at=_T1,
        )
    finally:
        conn_a.close()

    result = _sync(spoke_a, hub, "spoke-a")
    assert result.accepted >= 1

    hub_conn = _open(hub_dir)
    try:
        assert hub_conn.execute(
            "SELECT mode FROM sync_policies WHERE machine_id = ?",
            ("peer-x" + "0" * 26,),
        ).fetchone() == ("pinned",)
        assert hub_conn.execute(
            "SELECT name FROM machines WHERE machine_id = ?", ("peer-x" + "0" * 26,)
        ).fetchone() == ("peer-x",), "the pusher's fleet snapshot was not merged"
    finally:
        hub_conn.close()


def test_push_merges_the_snapshot_it_carries_and_refuses_without_one(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """``POST /push`` is the endpoint under test, not the client that calls it.

    Both halves matter: without the snapshot the FOREIGN KEY still bites (so
    the constraint is real and the test is not vacuous), and with it the same
    payload lands.
    """
    result = _sync(spoke_a, hub, "spoke-a")
    unknown = "peer-y" + "0" * 26
    policy = {
        "table": "sync_policies",
        "pk": [unknown, "audio"],
        "values": {
            "machine_id": unknown,
            "asset_kind": "audio",
            "mode": "cached",
            "cache_budget_mb": None,
            "updated_at": _T1,
            "origin_device_id": result.machine_id,
            "deleted_at": None,
        },
    }
    fleet = {
        "machine_id": unknown,
        "name": "peer-y",
        "platform": "linux",
        "is_hub": False,
        "data_root": None,
        "first_seen": _T0,
        "last_seen": _T0,
    }
    body: dict[str, object] = {
        "machine_id": result.machine_id,
        "schema_version": state_schema.SCHEMA_VERSION,
        "rows": [policy],
    }

    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.post(f"{client.API_PREFIX}/push", body)
    assert "FOREIGN KEY" in str(excinfo.value)

    accepted = hub.post(f"{client.API_PREFIX}/push", dict(body, machines=[fleet]))
    assert accepted["accepted"] == 1

    hub_conn = _open(hub_dir)
    try:
        assert hub_conn.execute(
            "SELECT mode FROM sync_policies WHERE machine_id = ?", (unknown,)
        ).fetchone() == ("cached",)
    finally:
        hub_conn.close()


def test_restore_recovery_carrying_a_peers_location_rows_lands(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """N4's sketch: the recovery push is the one that wedged.

    ADR 08 point 1 gave ``track_locations`` a ``machine_id`` FK and every
    spoke holds its peers' rows (that is what the fleet overview reads), and
    ADR 08 point 4 makes a restored hub trigger a full re-offer. Round 2
    observed ``n6 B re-offer after restore -> HTTP 409 ... FOREIGN KEY
    constraint failed`` on every retry: the recovery mechanism triggered the
    wedge.
    """
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    conn_a = _open(spoke_a)
    try:
        machine_a = sync_stamp.ensure_local_machine(conn_a)
        locations.upsert_location(
            conn_a,
            stable_id="trk-1",
            kind="local",
            file_path="/Music/a.mp3",
            role="primary",
        )
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    conn_b = _open(spoke_b)
    try:
        held = [row[1] for row in _locations(conn_b)]
    finally:
        conn_b.close()
    assert machine_a in held, "B never received A's location row; probe is void"

    # The hub is restored to a point before machine A ever said hello: no
    # rows of A's, no registration for A, no changelog.
    hub_conn = _open(hub_dir)
    try:
        hub_conn.execute("DELETE FROM track_locations")
        hub_conn.execute("DELETE FROM tracks")
        hub_conn.execute("DELETE FROM machines WHERE machine_id = ?", (machine_a,))
        hub_conn.execute("DELETE FROM hub_changelog")
    finally:
        hub_conn.close()

    recovered = _sync(spoke_b, hub, "spoke-b")
    assert recovered.hub_restore_detected is True
    assert recovered.rejected == 0, "the recovery push was refused"

    hub_conn = _open(hub_dir)
    try:
        assert hub_conn.execute(
            "SELECT COUNT(*) FROM track_locations WHERE machine_id = ?", (machine_a,)
        ).fetchone() == (1,), "A's location row did not survive the recovery"
    finally:
        hub_conn.close()


# ----- N5: every partial UNIQUE index is a resolution target -----------------


def _location_values(
    *,
    location_id: str,
    stable_id: str,
    machine_id: str,
    file_path: str | None,
    remote_url: str | None,
    updated_at: str,
    origin: str,
) -> dict[str, object]:
    """A complete ``track_locations`` row as the wire carries it."""
    return {
        "location_id": location_id,
        "stable_id": stable_id,
        "machine_id": machine_id,
        "kind": "local",
        "role": "alternate",
        "file_path": file_path,
        "remote_url": remote_url,
        "venue_key": None,
        "venue_rank": None,
        "available": 0,
        "probed_at": None,
        "content_hash": None,
        "created_at": _T0,
        "updated_at": updated_at,
        "origin_device_id": origin,
        "deleted_at": None,
    }


def test_a_collision_on_the_url_index_resolves_instead_of_409ing(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """N5's sketch: the collision is on the SECOND partial UNIQUE index.

    ``NATURAL_KEYS`` lists the ``file_path`` tuple first and round 2 resolved
    against the first all-non-NULL tuple only, so a row carrying both columns
    resolved on ``file_path`` while ``idx_track_locations_url`` was still
    enforced. Round 2 observed ``f3 attempt 0: HTTP 409 SYNC_APPLY ... UNIQUE
    constraint failed`` and an identical attempt 1 -- round 1 finding 1's
    exact shape, one index over.
    """
    _seed_common_track((spoke_a,), "trk-1")
    result = _sync(spoke_a, hub, "spoke-a")

    def _push(values: dict[str, object]) -> dict[str, object]:
        return hub.post(
            f"{client.API_PREFIX}/push",
            {
                "machine_id": result.machine_id,
                "schema_version": state_schema.SCHEMA_VERSION,
                "rows": [
                    {
                        "table": "track_locations",
                        "pk": [values["location_id"]],
                        "values": values,
                    }
                ],
            },
        )

    seeded = _location_values(
        location_id="b" * 32,
        stable_id="trk-1",
        machine_id=result.machine_id,
        file_path="/Music/a.mp3",
        remote_url="r2://audio/trk-1",
        updated_at=_T1,
        origin=result.machine_id,
    )
    assert _push(seeded)["accepted"] == 1

    # Same logical remote copy, re-minted id, and the file moved -- so the
    # path index does NOT collide and only the url index does.
    moved = _location_values(
        location_id="c" * 32,
        stable_id="trk-1",
        machine_id=result.machine_id,
        file_path="/Music/moved.mp3",
        remote_url="r2://audio/trk-1",
        updated_at=_T2,
        origin=result.machine_id,
    )
    assert _push(moved)["accepted"] == 1, "the url-index collision was not resolved"

    hub_conn = _open(hub_dir)
    try:
        assert [row[0] for row in _locations(hub_conn)] == ["c" * 32]
        assert protocol.encode_row_pk(("b" * 32,)) not in _changelog_pks(
            hub_conn, "track_locations"
        ), "the superseded row still has a changelog entry; every pull 409s"
    finally:
        hub_conn.close()


def test_a_row_losing_to_one_of_two_duplicates_is_rejected_whole(
    spoke_a: Path,
) -> None:
    """Two indexes, two different local rows, one incoming row.

    The incoming row must beat BOTH to land: dropping the one it beat while
    losing to the other would delete a row nothing replaces.
    """
    conn = _open(spoke_a)
    try:
        machine = sync_stamp.ensure_local_machine(conn)
        _insert_track(conn, "trk-1", title="t", updated_at=_T0, origin=_DEV_A)
        for location_id, path, url, stamp in (
            ("a" * 32, "/Music/a.mp3", "r2://audio/old", _T1),
            ("b" * 32, "/Music/b.mp3", "r2://audio/new", _T3),
        ):
            values = _location_values(
                location_id=location_id,
                stable_id="trk-1",
                machine_id=machine,
                file_path=path,
                remote_url=url,
                updated_at=stamp,
                origin=_DEV_A,
            )
            engine.spoke_apply(
                conn,
                [
                    protocol.RowChange(
                        table="track_locations",
                        pk=(location_id,),
                        values=dict(values),
                    )
                ],
            )
        assert len(_locations(conn)) == 2

        # Collides with 'a' on the path index (and outranks it) and with 'b'
        # on the url index (and loses to it).
        contested = _location_values(
            location_id="d" * 32,
            stable_id="trk-1",
            machine_id=machine,
            file_path="/Music/a.mp3",
            remote_url="r2://audio/new",
            updated_at=_T2,
            origin=_DEV_A,
        )
        applied = engine.spoke_apply(
            conn,
            [
                protocol.RowChange(
                    table="track_locations", pk=("d" * 32,), values=dict(contested)
                )
            ],
        )
        assert applied.rejected == 1
        assert [row[0] for row in _locations(conn)] == ["a" * 32, "b" * 32], (
            "a row that lost on one index still dropped the duplicate it beat "
            "on the other"
        )
    finally:
        conn.close()


def test_a_regular_pull_does_not_overwrite_a_newer_local_row(spoke_a: Path) -> None:
    """Hub authority is for identity collapse and the repair bundle only.

    A plain pulled row that is OLDER than the local copy of the same primary
    key must lose under row-level LWW (ADR-0004). Letting every pulled row win
    overwrites a local edit that has not reached the hub yet, and the edit is
    gone for good because the local stamp is replaced with the older one.
    """
    conn = _open(spoke_a)
    try:
        machine = sync_stamp.ensure_local_machine(conn)
        _insert_track(conn, "trk-1", title="t", updated_at=_T0, origin=_DEV_A)
        location_id = "a" * 32

        def location(url: str, stamp: str) -> protocol.RowChange:
            values = _location_values(
                location_id=location_id,
                stable_id="trk-1",
                machine_id=machine,
                file_path="/Music/a.mp3",
                remote_url=url,
                updated_at=stamp,
                origin=_DEV_A,
            )
            return protocol.RowChange(
                table="track_locations", pk=(location_id,), values=dict(values)
            )

        # POSITIVE CONTROL: a newer pulled row does land, so the probe can see
        # an overwrite at all.
        assert engine.spoke_apply(conn, [location("r2://audio/v1", _T1)]).accepted == 1
        assert engine.spoke_apply(conn, [location("r2://audio/local-edit", _T2)]).accepted == 1

        older = engine.spoke_apply(conn, [location("r2://audio/stale-hub", _T1)])
        assert older.rejected == 1
        stored = conn.execute(
            "SELECT remote_url FROM track_locations WHERE location_id = ?",
            (location_id,),
        ).fetchone()
        assert stored[0] == "r2://audio/local-edit", (
            "an older pulled row overwrote the newer local edit"
        )
    finally:
        conn.close()


# ----- 4a blast radius: a changelog entry whose row is gone -----------------


def test_a_hard_deleted_hub_row_does_not_take_every_pull_offline(
    hub: _TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    spoke_b: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The r1 probe: one manual DELETE on the hub, every spoke bricked.

    Round 2 observed ``r1 attempt 0/1: HTTP 409 "hub_changelog points at
    tracks row [\\"trk-1\\"] which no longer exists"`` -- the same 409 on
    every retry, for every spoke, with no repair path. The entry is
    bookkeeping and the row it names is gone, so there is nothing to
    serialize: the pull skips it, says how many it skipped, and shouts in
    the hub's log.
    """
    _seed_common_track((spoke_a,), "trk-gone")
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-kept", title="kept", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")

    hub_conn = _open(hub_dir)
    try:
        # Not a tombstone: a hard DELETE, which ADR 04 c4 forbids and round 1
        # finding 4a found in shipped UI code.
        hub_conn.execute("DELETE FROM tracks WHERE stable_id = 'trk-gone'")
    finally:
        hub_conn.close()

    with caplog.at_level("ERROR", logger="apps.sync_hub.engine"):
        received = _sync(spoke_b, hub, "spoke-b")
    assert received.applied >= 1, "the surviving rows did not reach B"
    assert any(
        "which no longer exists" in record.getMessage() for record in caplog.records
    ), "the hub dropped an entry silently"

    conn_b = _open(spoke_b)
    try:
        assert _track_title(conn_b, "trk-kept") == "kept"
        assert _track_title(conn_b, "trk-gone") is None
    finally:
        conn_b.close()

    # And it stays serviceable: a second spoke, and a second sync, both work.
    assert _sync(spoke_b, hub, "spoke-b").pulled >= 0


def test_the_pull_reports_how_many_entries_it_skipped(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """``skipped`` is observed on the wire, not inferred from a log line."""
    _seed_common_track((spoke_a,), "trk-gone")
    result = _sync(spoke_a, hub, "spoke-a")

    hub_conn = _open(hub_dir)
    try:
        hub_conn.execute("DELETE FROM tracks WHERE stable_id = 'trk-gone'")
    finally:
        hub_conn.close()

    payload = hub.get(
        f"{client.API_PREFIX}/pull",
        {"machine_id": result.machine_id, "since_seq": "0"},
    )
    assert payload["skipped"] == 1
    assert payload["rows"] == []
