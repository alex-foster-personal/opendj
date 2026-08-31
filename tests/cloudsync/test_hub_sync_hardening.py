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
"""
from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import locations, provenance
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.sync_hub import client, engine, protocol, service
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _DEV_B,
    _T0,
    _T1,
    _T2,
    _T3,
    _TestClientTransport,
    _insert_track,
    _log,
    _open,
    _seed_common_track,
    _set_track_title,
    _sync,
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
