"""Round 2 hardening: the round 1 adversarial findings, as permanent tests.

Every test here is one reproduction sketch from
``.planning/cloudsync-round1-adversarial.md`` turned into a regression, and
the fix it guards is specified in ``specs/design_decision_08.md``. The
mapping, so a failure names the defect it just let back in:

- finding 1 / 7a  -> two machines minting a ``track_locations`` row for the
  same file wedged the push with a UNIQUE-violation 409 that re-fired on
  every retry. ADR 08 point 1: ``machine_id`` joins the identity and the
  engine resolves on the natural key.
- finding 2       -> ISO8601 string comparison is not an ordering over
  instants. ADR 08 point 2: the boundary normalizes, or 422s.
- finding 3       -> the push watermark was a wall clock taken over PULLED
  rows, so one peer with a skewed clock silently dropped this machine's
  edits. ADR 08 point 3: the fence is ``local_changelog.seq``.
- finding 6a      -> NFD and NFC spellings of one path diverged the digest.
- finding 7b      -> ``/pull`` and ``/status`` served anyone.
- A1              -> no writer stamped ``updated_at``, so the second
  ``track_fields`` edit ever made bricked the machine.
- A2              -> ``/pull`` and ``/push`` were unpaginated.
- A3              -> a hub restored from a point in time never got its rows
  back, because the spoke's floors survived the restore.

Acceptance criteria, one test each:
- if two machines holding the same file cannot both sync, per-machine
  locations are not per-machine -- broken.
- if a spoke that re-minted a ``location_id`` gets a 409, the engine is still
  keying on the primary key -- broken.
- if a peer stamped in 2099 stops this machine's ordinary edit from being
  offered, a clock is still a watermark -- broken.
- if the three observed ISO8601 orderings do not compare as their instants
  do, normalization is not happening at the boundary -- broken.
- if an unorderable timestamp reaches a table instead of a 422, the boundary
  is not fail-fast -- broken.
- if a restored hub does not get its rows back, ADR 04 c3 recovery does not
  exist -- broken.
- if ``/pull`` or ``/status`` answers a machine that never said hello, the
  consistency guard is push-only -- broken.
- if a few hundred rows cross in one request, the wire is unpaginated and a
  first sync is tens of MB in one body -- broken.
- if two spellings of one path produce two digests, converged peers halt
  each other -- broken.
- if a second ``provenance.write_field`` edit does not converge, the writers
  are not stamping and ``track_fields`` sync is dead -- broken.
- if a membership write in the changelog is offered as itself rather than as
  its playlist row, the push raises on a table that is not pushable -- broken.
- if a push carrying a row for a machine the hub has not met is refused, the
  fleet snapshot is not travelling with the push and hub restore wedges the
  recovery it is supposed to be (round 2 N4/A4) -- broken.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import locations, provenance, sync_stamp
from apps.shared.state import schema as state_schema
from apps.sync_hub import (
    client,
    engine,
    generation,
    maintenance,
    protocol,
    service,
)
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _DEV_B,
    _T0,
    _T1,
    _T2,
    _T3,
    _insert_playlist,
    _insert_track,
    _log,
    _members,
    _open,
    _seed_common_track,
    _set_members,
    _set_track_title,
    _sync,
    _TestClientTransport,
    _track_title,
)

pytestmark = pytest.mark.requirement("CAT-04")

# The skewed peer from finding 3's reproduction: a real spoke whose clock is
# decades ahead, which is all it took to lose another machine's edits.
_T_SKEWED = "2099-01-01T00:00:00.000000+00:00"

# One file, two correct spellings (finding 6a). macOS hands back the first
# (o + combining diaeresis), Windows and Linux the second (o-umlaut). Written
# as an escape so the difference survives any editor that helpfully
# normalizes this source file.
_PATH_RAW = "/Music/Bj\u00f6rk - Joga.mp3"
_PATH_NFD = unicodedata.normalize("NFD", _PATH_RAW)
_PATH_NFC = unicodedata.normalize("NFC", _PATH_RAW)


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


# ----- helpers -------------------------------------------------------------


def _locations(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Every location row as ``(location_id, machine_id, file_path)``."""
    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            "SELECT location_id, machine_id, file_path FROM track_locations "
            "ORDER BY location_id"
        )
    ]


def _changelog_pks(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT row_pk FROM hub_changelog WHERE table_name = ? ORDER BY seq",
            (table,),
        )
    ]


def _insert_location(
    conn: sqlite3.Connection,
    *,
    location_id: str,
    stable_id: str,
    machine_id: str,
    file_path: str,
    updated_at: str,
    origin: str,
) -> None:
    """A location row with a caller-chosen ``location_id``.

    ``locations.upsert_location`` deliberately makes a duplicate key
    impossible, so reproducing the divergent-key shape needs the raw insert.
    Everything else about the row -- the stamp, the changelog entry -- is
    exactly what the writer would have produced.
    """
    stamped = _log(conn, "track_locations", (location_id,), origin, updated_at)
    conn.execute(
        "INSERT INTO track_locations("
        "location_id, stable_id, machine_id, kind, role, file_path, "
        "available, created_at, updated_at, origin_device_id) "
        "VALUES (?, ?, ?, 'local', 'primary', ?, 0, ?, ?, ?)",
        (location_id, stable_id, machine_id, file_path, _T0, stamped, origin),
    )


# ----- finding 1 / 7a: per-machine locations --------------------------------


def test_two_machines_holding_one_file_both_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """Finding 1's sketch: same track, same path, two random location ids.

    Round 1 observed ``POST /push -> HTTP 409 ... UNIQUE constraint failed:
    track_locations.stable_id, kind, file_path`` on the second spoke, on
    every retry, forever. The rows are per-machine now, so both land.
    """
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    for data_dir in (spoke_a, spoke_b):
        conn = _open(data_dir)
        try:
            locations.upsert_location(
                conn,
                stable_id="trk-1",
                kind="local",
                file_path="/Music/a.mp3",
                role="primary",
            )
        finally:
            conn.close()

    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    hub_conn = _open(hub_dir)
    try:
        rows = _locations(hub_conn)
    finally:
        hub_conn.close()
    assert len(rows) == 2, f"one row per machine expected, got {rows}"
    assert len({machine_id for _, machine_id, _ in rows}) == 2
    assert {path for _, _, path in rows} == {"/Music/a.mp3"}

    # 7a's other half: B sees A's row but must never count it as its own.
    conn_b = _open(spoke_b)
    try:
        assert len(_locations(conn_b)) == 2, "B did not receive A's row"
        assert locations.list_location_paths(conn_b, ["trk-1"]) == {
            "trk-1": ["/Music/a.mp3"]
        }, "B counted A's file as locally present"
    finally:
        conn_b.close()


def test_a_relocated_location_id_merges_instead_of_wedging(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """One logical row, two ``location_id`` values, one machine.

    The residual shape of finding 1 once ``machine_id`` is in the key: a
    spoke whose DB was restored and re-migrated mints a fresh
    ``location_id`` for a natural key the hub already holds. Keying the
    upsert on ``location_id`` turns that into the same permanent 409, so the
    engine resolves on the natural key and the loser is dropped from the
    changelogs with it.
    """
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    conn_a = _open(spoke_a)
    try:
        machine_a = sync_stamp.ensure_local_machine(conn_a)
        _insert_location(
            conn_a,
            location_id="a" * 32,
            stable_id="trk-1",
            machine_id=machine_a,
            file_path="/Music/a.mp3",
            updated_at=_T1,
            origin=machine_a,
        )
    finally:
        conn_a.close()

    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    # A is restored and re-migrated: same file, brand new location_id. A
    # restored DB carries neither the old row nor its changelog entry.
    conn_a = _open(spoke_a)
    try:
        conn_a.execute("DELETE FROM track_locations WHERE location_id = ?", ("a" * 32,))
        conn_a.execute(
            "DELETE FROM local_changelog WHERE table_name = 'track_locations'"
        )
        _insert_location(
            conn_a,
            location_id="c" * 32,
            stable_id="trk-1",
            machine_id=machine_a,
            file_path="/Music/a.mp3",
            updated_at=_T2,
            origin=machine_a,
        )
    finally:
        conn_a.close()

    result = _sync(spoke_a, hub, "spoke-a")
    assert result.accepted >= 1

    hub_conn = _open(hub_dir)
    try:
        assert [row[0] for row in _locations(hub_conn)] == ["c" * 32]
        assert protocol.encode_row_pk(("a" * 32,)) not in _changelog_pks(
            hub_conn, "track_locations"
        ), "the superseded row still has a changelog entry; every pull 409s"
    finally:
        hub_conn.close()

    # B, which already holds the old id, must converge on the new one rather
    # than keeping a second row for the same file.
    _sync(spoke_b, hub, "spoke-b")
    conn_b = _open(spoke_b)
    try:
        assert [row[0] for row in _locations(conn_b)] == ["c" * 32]
    finally:
        conn_b.close()


def test_a_duplicate_natural_key_tie_resolves_the_same_way_on_both_peers(
    spoke_a: Path,
) -> None:
    """Equal stamps, two ids: the survivor is the smaller key, both sides.

    Without a deterministic tiebreak the two rows reject each other forever
    and the digest never matches -- the shape of finding 5b, one table over.
    Applying the same pair in both directions must pick the same winner.
    """
    conn = _open(spoke_a)
    try:
        machine = sync_stamp.ensure_local_machine(conn)
        _insert_track(conn, "trk-1", title="t", updated_at=_T0, origin=_DEV_A)
        _insert_location(
            conn,
            location_id="b" * 32,
            stable_id="trk-1",
            machine_id=machine,
            file_path="/Music/a.mp3",
            updated_at=_T1,
            origin=_DEV_A,
        )
        columns = protocol.table_columns(conn, "track_locations")
        stored = conn.execute(
            f"SELECT {', '.join(columns)} FROM track_locations"
        ).fetchone()
        values = dict(protocol.canonical_row("track_locations", columns, stored))

        lower = dict(values, location_id="a" * 32)
        engine.spoke_apply(
            conn,
            [
                protocol.RowChange(
                    table="track_locations", pk=("a" * 32,), values=lower
                )
            ],
        )
        assert [row[0] for row in _locations(conn)] == ["a" * 32]

        higher = dict(values, location_id="d" * 32)
        engine.spoke_apply(
            conn,
            [
                protocol.RowChange(
                    table="track_locations", pk=("d" * 32,), values=higher
                )
            ],
        )
        assert [row[0] for row in _locations(conn)] == [
            "a" * 32
        ], "the higher key won on a tie; two peers would disagree"
    finally:
        conn.close()


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


# ----- finding 3: the push fence -------------------------------------------


def test_a_peer_with_a_skewed_clock_cannot_swallow_a_local_edit(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """Finding 3's sketch, verbatim: B writes in 2099, A loses an edit.

    Round 1 observed ``A push floor after pull: 2099-01-01T00:00:00+00:00``
    and ``hub title for 'seed': ('s',)`` -- A's own edit was never sent, not
    rejected. The floor is a sequence number now, so A's edit is offered
    whatever B's clock says.
    """
    _seed_common_track((spoke_a, spoke_b), "seed")
    conn_b = _open(spoke_b)
    try:
        _insert_track(
            conn_b, "skew", title="future row", updated_at=_T_SKEWED, origin=_DEV_B
        )
    finally:
        conn_b.close()

    _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")  # A pulls the 2099 row

    conn_a = _open(spoke_a)
    try:
        _set_track_title(
            conn_a, "seed", title="ordinary edit on A", updated_at=_T3, origin=_DEV_A
        )
    finally:
        conn_a.close()

    result = _sync(spoke_a, hub, "spoke-a")
    assert result.pushed == 1, (
        f"A should offer exactly the one row it edited, offered {result.pushed}. "
        f"0 means a clock raised the floor over it (finding 3); more means the "
        f"fence is not fencing and every sync re-offers the library."
    )
    assert result.accepted == 1

    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "seed") == "ordinary edit on A"
    finally:
        hub_conn.close()


def test_an_unstamped_edit_is_never_silently_offered(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """The fence's other edge: a raw write is invisible to the push.

    ADR 08 consequence 2 accepts this explicitly -- the stamp helper is a
    choke point, and a writer that bypasses it produces rows the fence
    cannot see. What must NOT happen is a quiet success: the digest compare
    is the backstop, and it has to fire.
    """
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")

    conn_a = _open(spoke_a)
    try:
        conn_a.execute(
            "UPDATE tracks SET title = ?, updated_at = ? WHERE stable_id = ?",
            ("bypassed the writer", _T3, "trk-1"),
        )
    finally:
        conn_a.close()

    with pytest.raises(client.SyncDigestMismatch):
        _sync(spoke_a, hub, "spoke-a")


def test_a_membership_write_is_offered_as_its_playlist_row(
    hub: _TestClientTransport, spoke_a: Path, spoke_b: Path
) -> None:
    """A logged membership row is pushable only through its parent.

    ``StateWriter.set_playlist_memberships`` stamps and logs the membership
    rows it touches -- it must, or the fence cannot see the write -- but
    ``playlist_memberships`` is not a pushable table (ADR 04 c5), so the
    push resolves each entry back to the ``playlists`` row that carries the
    whole bundle. Offering it as itself raises ``'playlist_memberships' is
    not in the sync set`` and the spoke stops syncing entirely.
    """
    _seed_common_track((spoke_a, spoke_b), "trk-1")
    _seed_common_track((spoke_a, spoke_b), "trk-2")
    conn_a = _open(spoke_a)
    try:
        _insert_playlist(conn_a, "pl-1", name="OLTF", updated_at=_T1, origin=_DEV_A)
        _set_members(conn_a, "pl-1", ("trk-1",), updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    conn_a = _open(spoke_a)
    try:
        # Only the membership rows are logged; the playlist row is stamped in
        # place, exactly as the writer leaves it.
        conn_a.execute("DELETE FROM playlist_memberships WHERE playlist_id = 'pl-1'")
        stamped = _log(conn_a, "playlist_memberships", ("pl-1", 0), _DEV_A, _T2)
        conn_a.execute(
            "INSERT INTO playlist_memberships("
            "playlist_id, stable_id, position, updated_at, origin_device_id) "
            "VALUES ('pl-1', 'trk-2', 0, ?, ?)",
            (stamped, _DEV_A),
        )
        conn_a.execute(
            "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
            "WHERE playlist_id = 'pl-1'",
            (stamped, _DEV_A),
        )
    finally:
        conn_a.close()

    assert _sync(spoke_a, hub, "spoke-a").accepted == 1
    _sync(spoke_b, hub, "spoke-b")
    conn_b = _open(spoke_b)
    try:
        assert _members(conn_b, "pl-1") == ("trk-2",)
    finally:
        conn_b.close()


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


# ----- finding 2: timestamp normalization ----------------------------------


def test_the_three_broken_orderings_now_compare_as_instants() -> None:
    """The three ``[observed]`` comparisons from finding 2, all fixed.

    Each line was ``True`` in round 1 under plain string comparison, and each
    was wrong: the same instant, or an earlier one, sorting above a real
    edit.
    """
    base = protocol.lww_key({"updated_at": "2026-08-30T10:00:00+00:00"})
    zulu = protocol.lww_key({"updated_at": "2026-08-30T10:00:00Z"})
    micros = protocol.lww_key({"updated_at": "2026-08-30T10:00:00.000001+00:00"})
    offset = protocol.lww_key({"updated_at": "2026-08-30T11:00:00+01:00"})

    assert zulu == base, "a Z suffix still outsorts the identical instant"
    assert micros > base, "one microsecond later must still sort later"
    assert offset == base, "a +01:00 offset still misplaces the instant"
    assert base[0] == "2026-08-30T10:00:00.000000+00:00"


@pytest.mark.parametrize(
    "bad",
    ["not-a-timestamp", "2026-08-30T10:00:00", "", "2026-13-01T00:00:00+00:00"],
)
def test_an_unorderable_updated_at_is_refused_with_422(
    hub: _TestClientTransport, spoke_a: Path, bad: str
) -> None:
    """A naive or unparseable stamp is a 422, not a row in the table."""
    result = _sync(spoke_a, hub, "spoke-a")
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.post(
            f"{client.API_PREFIX}/push",
            {
                "machine_id": result.machine_id,
                "schema_version": state_schema.SCHEMA_VERSION,
                "rows": [
                    {
                        "table": "tracks",
                        "pk": ["trk-bad"],
                        "values": {
                            "stable_id": "trk-bad",
                            "stable_id_tier": "inferred",
                            "title": "bad stamp",
                            "artists_json": None,
                            "album": None,
                            "duration_ms": None,
                            "isrc": None,
                            "file_path": None,
                            "folder_path": None,
                            "created_at": _T0,
                            "updated_at": bad,
                            "origin_device_id": _DEV_A,
                            "deleted_at": None,
                        },
                    }
                ],
            },
        )
    assert "422" in str(excinfo.value)
    assert "SYNC_PROTOCOL" in str(excinfo.value)


def test_a_differently_spelled_equal_instant_does_not_win(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """``Z`` and ``+00:00`` at one instant must tie, not overwrite."""
    _seed_common_track((spoke_a,), "trk-1")
    conn_a = _open(spoke_a)
    try:
        _set_track_title(conn_a, "trk-1", title="stored", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    result = _sync(spoke_a, hub, "spoke-a")

    hub_conn = _open(hub_dir)
    try:
        columns = protocol.table_columns(hub_conn, "tracks")
        row = hub_conn.execute(
            f"SELECT {', '.join(columns)} FROM tracks WHERE stable_id = 'trk-1'"
        ).fetchone()
    finally:
        hub_conn.close()
    values = dict(protocol.canonical_row("tracks", columns, row))
    values["title"] = "should not win"
    values["updated_at"] = "2026-08-30T10:00:00Z"

    pushed = hub.post(
        f"{client.API_PREFIX}/push",
        {
            "machine_id": result.machine_id,
            "schema_version": state_schema.SCHEMA_VERSION,
            "rows": [{"table": "tracks", "pk": ["trk-1"], "values": values}],
        },
    )
    assert pushed["rejected"] == 1, "a Z-spelled equal instant overwrote the row"

    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-1") == "stored"
    finally:
        hub_conn.close()


# ----- A3: hub restore ------------------------------------------------------


def test_a_restored_hub_resets_the_floors_and_recovers(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """A3's sketch: the hub loses rows, the spoke must re-offer everything.

    Round 1 observed ``hub after re-sync: 1 track`` (only the row sitting on
    the inclusive floor) and a bricked spoke. ``hello`` reports the hub's max
    seq now, and a spoke whose pull floor is above it drops both floors.
    """
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
        _insert_track(conn_a, "trk-2", title="two", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    first = _sync(spoke_a, hub, "spoke-a")
    assert first.accepted == 2
    assert first.hub_restore_detected is False

    hub_conn = _open(hub_dir)
    try:
        hub_conn.execute("DELETE FROM tracks")
        hub_conn.execute("DELETE FROM hub_changelog")
    finally:
        hub_conn.close()

    recovered = _sync(spoke_a, hub, "spoke-a")
    assert recovered.hub_restore_detected is True
    assert recovered.accepted == 2, "the spoke did not re-offer the whole library"

    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-1") == "one"
        assert _track_title(hub_conn, "trk-2") == "two"
    finally:
        hub_conn.close()


# ----- N6: the generation token and changelog retention ---------------------


def _changelog(conn: sqlite3.Connection, table: str) -> list[tuple[int, str, str]]:
    return [
        (int(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            f"SELECT seq, table_name, row_pk FROM {table} ORDER BY seq"
        )
    ]


def test_a_changelog_prune_is_not_mistaken_for_a_restore(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """N6's sketch: maintenance must not cost a fleet-wide re-offer.

    Round 2 observed ``n5b changelog-only prune -> restore_detected=True
    pushed=1`` -- any prune, vacuum or table-scoped repair made every spoke
    log ``the hub went BACKWARDS`` at ERROR and re-offer its whole library
    (minutes per 8k tracks, ADR 08 consequence 3). The token does not move
    when entries are pruned, and the sanctioned prune cannot lower
    ``MAX(seq)`` because it never drops the newest entry for a row.
    """
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
        _insert_track(conn_a, "trk-2", title="other", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")
    # A second edit in a second sync: one row, two hub_changelog entries, the
    # older of which is superseded and therefore prunable.
    conn_a = _open(spoke_a)
    try:
        _set_track_title(conn_a, "trk-1", title="three", updated_at=_T2, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    hub_conn = _open(hub_dir)
    try:
        before = _changelog(hub_conn, "hub_changelog")
        deleted = engine.prune_changelog(hub_conn, keep_days=0.0, keep_rows=0)
        after = _changelog(hub_conn, "hub_changelog")
    finally:
        hub_conn.close()

    assert deleted >= 1, f"nothing was prunable out of {before}"
    assert max(seq for seq, _, _ in after) == max(seq for seq, _, _ in before), (
        "the prune lowered MAX(seq); that is indistinguishable from a restore"
    )
    assert {(table, pk) for _, table, pk in after} == {
        (table, pk) for _, table, pk in before
    }, "the prune dropped a row's ONLY entry; that row can never be pulled again"

    quiet = _sync(spoke_a, hub, "spoke-a")
    assert quiet.hub_restore_detected is False, "a prune was read as a restore"
    assert quiet.pushed == 0, "the spoke re-offered its library after a prune"

    # And a spoke starting from scratch still gets everything the hub holds.
    fresh = _sync(spoke_b, hub, "spoke-b")
    assert fresh.hub_restore_detected is False
    conn_b = _open(spoke_b)
    try:
        assert _track_title(conn_b, "trk-1") == "three"
        assert _track_title(conn_b, "trk-2") == "other"
    finally:
        conn_b.close()


def test_prune_keeps_entries_inside_the_retention_window(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """Superseded is necessary but not sufficient: the bounds bind too."""
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")
    conn_a = _open(spoke_a)
    try:
        _set_track_title(conn_a, "trk-1", title="two", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")

    hub_conn = _open(hub_dir)
    try:
        assert len(_changelog(hub_conn, "hub_changelog")) == 2, (
            "the probe needs one superseded entry to be about anything"
        )
        assert (
            engine.prune_changelog(hub_conn, keep_days=3650.0, keep_rows=0) == 0
        ), "an entry inside the day window was pruned"
        assert (
            engine.prune_changelog(hub_conn, keep_days=0.0, keep_rows=10_000) == 0
        ), "an entry inside the row window was pruned"
        assert (
            engine.prune_changelog(hub_conn, keep_days=0.0, keep_rows=0, now=_T3) == 0
        ), "an entry newer than the supplied cutoff was pruned"
        # Not vacuous: with both bounds open, the superseded entry does go.
        assert engine.prune_changelog(hub_conn, keep_days=0.0, keep_rows=0) == 1
        with pytest.raises(engine.SyncApplyError):
            engine.prune_changelog(hub_conn, changelog="tracks")
    finally:
        hub_conn.close()


def test_a_rotated_generation_is_what_triggers_the_re_offer(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """The restore runbook's own signal, for a restore the anchor cannot see.

    A whole-machine restore rolls the data dir back with the DB, so the
    anchor agrees with the DB and nothing looks wrong. Rotating by hand is
    then the signal, and it must produce the same recovery as an automatic
    rotation.
    """
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
    finally:
        conn_a.close()
    first = _sync(spoke_a, hub, "spoke-a")
    assert first.hub_restore_detected is False

    before = maintenance.show_generation(hub_dir)
    after = maintenance.rotate(hub_dir)
    assert after != before

    recovered = _sync(spoke_a, hub, "spoke-a")
    assert recovered.hub_restore_detected is True
    assert recovered.pushed >= 1, "the re-offer did not happen"
    assert _sync(spoke_a, hub, "spoke-a").hub_restore_detected is False, (
        "the rotation was detected twice; the spoke did not store the new token"
    )


def test_the_generation_anchor_survives_an_ordinary_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """The token is stable across syncs, and the spoke stores what it saw."""
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")
    minted = maintenance.show_generation(hub_dir)
    result = _sync(spoke_a, hub, "spoke-a")

    assert maintenance.show_generation(hub_dir) == minted
    conn_a = _open(spoke_a)
    try:
        watermark = engine.read_watermark(conn_a, result.hub_machine_id)
    finally:
        conn_a.close()
    assert watermark.peer_generation == minted
    assert generation.anchor_path(hub_dir).exists()


def test_the_maintenance_cli_prunes_and_rotates(
    hub: _TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Agent-native parity: the operator actions are drivable without a UI."""
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
        _set_track_title(conn_a, "trk-1", title="two", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")

    assert (
        maintenance.main(
            [
                "prune",
                "--data-dir",
                str(hub_dir),
                "--keep-days",
                "0",
                "--keep-rows",
                "0",
            ]
        )
        == 0
    )
    assert "pruned" in capsys.readouterr().out

    assert maintenance.main(["generation", "--data-dir", str(hub_dir)]) == 0
    shown = capsys.readouterr().out.strip()
    assert maintenance.main(["rotate", "--data-dir", str(hub_dir)]) == 0
    assert capsys.readouterr().out.strip() != shown


# ----- finding 7b: registration on every endpoint ---------------------------


def test_pull_refuses_a_machine_that_never_said_hello(
    hub: _TestClientTransport,
) -> None:
    """An unregistered ``/pull`` returned the whole changelog in round 1."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(
            f"{client.API_PREFIX}/pull",
            {"machine_id": "deadbeef" * 4, "since_seq": "0"},
        )
    assert "SYNC_UNKNOWN_MACHINE" in str(excinfo.value)


def test_status_refuses_a_machine_that_never_said_hello(
    hub: _TestClientTransport,
) -> None:
    """``/status`` leaks every machine's absolute ``data_root``."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(f"{client.API_PREFIX}/status", {"machine_id": "deadbeef" * 4})
    assert "SYNC_UNKNOWN_MACHINE" in str(excinfo.value)


def test_pull_without_a_machine_id_is_refused(hub: _TestClientTransport) -> None:
    """The guard cannot be skipped by omitting the parameter."""
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(f"{client.API_PREFIX}/pull", {"since_seq": "0"})
    assert "422" in str(excinfo.value)


# ----- A2: pagination -------------------------------------------------------


def test_a_few_hundred_rows_cross_in_several_chunks(
    hub: _TestClientTransport,
    spoke_a: Path,
    spoke_b: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both directions chunk, and the whole library still converges.

    ``PULL_LIMIT`` is lowered so the loop runs several times over a test-sized
    library; ``PUSH_BATCH_ROWS`` is left alone and 300 rows split on it
    naturally. Convergence is the real assertion -- a chunked transfer that
    dropped a chunk would still make several requests.
    """
    monkeypatch.setattr(client, "PULL_LIMIT", 100)
    total = 300
    conn_a = _open(spoke_a)
    try:
        for index in range(total):
            _insert_track(
                conn_a,
                f"trk-{index:04d}",
                title=f"track {index}",
                updated_at=_T1,
                origin=_DEV_A,
            )
    finally:
        conn_a.close()

    pushed = _sync(spoke_a, hub, "spoke-a")
    assert pushed.pushed == total
    assert pushed.accepted == total
    assert pushed.push_requests == 2, (
        f"{total} rows at {client.PUSH_BATCH_ROWS}/request should be 2 requests, "
        f"got {pushed.push_requests}"
    )
    assert pushed.pull_requests >= 3, "the pull did not loop"

    received = _sync(spoke_b, hub, "spoke-b")
    assert received.pull_requests >= 3
    assert received.applied == total

    conn_b = _open(spoke_b)
    try:
        count = conn_b.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    finally:
        conn_b.close()
    assert count == total, "a chunk went missing"


# ----- finding 6a: Unicode ---------------------------------------------------


def test_two_spellings_of_one_path_hash_the_same(
    spoke_a: Path, spoke_b: Path
) -> None:
    """NFD on the Mac, NFC on Windows: one file, one digest.

    Round 1 observed ``hub track_locations: 2 rows for ONE file`` and two
    different digests, so a Mac and a Windows spoke could never agree.
    """
    assert _PATH_NFD != _PATH_NFC, "pick a path that actually differs by form"
    digests: list[str] = []
    for data_dir, path in ((spoke_a, _PATH_NFD), (spoke_b, _PATH_NFC)):
        conn = _open(data_dir)
        try:
            # Every column except the spelling is pinned identical across the
            # two DBs, so a difference in the digest can only be the path.
            conn.execute(
                "INSERT INTO machines(machine_id, name, platform, is_hub, "
                "data_root, first_seen, last_seen) "
                "VALUES ('fixed', 'fixed', 'macos', 0, NULL, ?, ?)",
                (_T0, _T0),
            )
            _insert_track(conn, "trk-1", title="t", updated_at=_T0, origin=_DEV_A)
            _insert_location(
                conn,
                location_id="e" * 32,
                stable_id="trk-1",
                machine_id="fixed",
                file_path=path,
                updated_at=_T1,
                origin=_DEV_A,
            )
            digests.append(protocol.table_digest(conn, "track_locations"))
        finally:
            conn.close()
    assert digests[0] == digests[1], "NFD and NFC spellings still diverge"


# ----- A1: the writers stamp -------------------------------------------------


def test_provenance_edits_converge_edit_sync_edit_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """A1's sketch, end to end: set a field, sync, set it again, sync.

    Round 1 observed ``second sync: BRICKED, SyncDigestMismatch, tables
    ['track_fields']`` and ``hub value: [('120', None, None)]`` -- the write
    left ``updated_at`` NULL, so the second edit presented the same LWW key
    and was rejected forever. The value must now move, and reach B.
    """
    _seed_common_track((spoke_a, spoke_b), "trk-1")

    conn_a = _open(spoke_a)
    try:
        provenance.write_field(
            conn_a,
            stable_id="trk-1",
            field_name="bpm",
            value=120.0,
            source="webui",
            modified_at=_T1,
        )
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")

    conn_a = _open(spoke_a)
    try:
        provenance.write_field(
            conn_a,
            stable_id="trk-1",
            field_name="bpm",
            value=174.0,
            source="webui",
            modified_at=_T2,
        )
    finally:
        conn_a.close()
    second = _sync(spoke_a, hub, "spoke-a")
    assert second.accepted >= 1, "the second edit was rejected; A1 is back"

    _sync(spoke_b, hub, "spoke-b")
    for data_dir in (hub_dir, spoke_b):
        conn = _open(data_dir)
        try:
            row = conn.execute(
                "SELECT value_json, updated_at, origin_device_id FROM track_fields "
                "WHERE stable_id = 'trk-1' AND field_name = 'bpm'"
            ).fetchone()
            assert row is not None, data_dir.name
            assert row[0] == "174.0", data_dir.name
            assert row[1] is not None, f"{data_dir.name} stored a NULL updated_at"
            assert row[2] is not None, f"{data_dir.name} stored a NULL origin"
        finally:
            conn.close()
