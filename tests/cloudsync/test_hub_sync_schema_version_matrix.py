"""The schema-version matrix: a spoke one version BEHIND and one AHEAD (plan W8 item 1).

[if] a version-mismatched peer gets past the handshake [then] fail, [else stop].

Before this module, only a NEWER spoke's ``/hello`` was tested. ADR-0012
calls v9 a fleet-wide flag day, and a regression in the refusal path would
corrupt or wedge mid-row instead of failing at the handshake. So every cell
below is parametrized over ``SCHEMA_VERSION - 1`` and ``SCHEMA_VERSION + 1``,
and every refusal carries a positive control: the same request at the hub's
own version succeeds against the same fixture, so a refusal cannot pass
because the endpoint refuses everything.

What the wire actually carries, pinned here rather than assumed:

* ``/hello`` and ``/push`` carry ``schema_version`` and refuse a mismatch
  with 409 ``SYNC_SCHEMA_VERSION`` before touching the database.
* ``/pull`` and ``/digest`` carry NO version at all. They are guarded only
  indirectly: a spoke refused at ``/hello`` was never registered, so both
  answer 409 ``SYNC_UNKNOWN_MACHINE``. A spoke that registered and whose
  BUILD later moved to another version is served by both, which
  :func:`test_a_registered_spoke_on_another_build_version_is_still_served`
  pins as a known gap (``private issue evidence unavailable in this copy``), not as a design.
* Since X2 (#1999) split WIRE_VERSION from SCHEMA_VERSION, ``schema_version``
  alone no longer gates the handshake when ``wire_version`` is present and
  matches: a same-wire, different-schema peer syncs
  (``test_hub_sync_wire_version.py`` pins that as the design). The two
  tests below named for "wire" therefore move ``wire_version``, not just
  ``schema_version``, to keep proving a refusal that still happens.
* The client checks the hub's reported version itself and raises
  :class:`~apps.sync_hub.client.SyncVersionMismatch` before any push. A real
  hub on this build refuses at ``/hello`` first, so that check only fires
  against a hub that answers without gating (an older build, a proxy); the
  test reaches it by rewriting the hello answer in flight.

Acceptance, one test each:
- if a hello one schema version away is accepted or registers the spoke then broken
- if a push one schema version away lands a row or a changelog entry then broken
- if a spoke refused at hello can still pull or read the digest then broken
- if pull or digest start gating a registered spoke's build version unnoticed then broken
- if run_sync from a spoke one schema version away sends anything past hello then broken
- if the client pushes to a hub that reports another schema version then broken
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, client_transport_ops, protocol, service, wire_version

from .enrollment_helpers import machine_payload
from .enrollment_transport import TestClientTransport
from .fault_transport import FaultTransport
from .test_hub_sync import _T3, _open, _seed_common_track, _track_title

pytestmark = pytest.mark.requirement("CLOUDSYNC-06")

VERSION_OFFSETS: tuple[int, ...] = (-1, 1)
OFFSET_IDS: tuple[str, ...] = ("one-behind", "one-ahead")
HELLO_PATH: str = f"{client.API_PREFIX}/hello"
PUSH_PATH: str = f"{client.API_PREFIX}/push"


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    path = tmp_path / "spoke-a"
    path.mkdir()
    return path


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[TestClientTransport]:
    """The real sync router on an empty hub DB; only the socket is absent."""
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield TestClientTransport(http)


# ----- helpers -------------------------------------------------------------


def _hello(hub: TestClientTransport, spoke: Path, version: int) -> dict[str, Any]:
    return hub.post(
        HELLO_PATH,
        {"machine": machine_payload(spoke, name="versioned-spoke"), "schema_version": version},
    )


def _registered_on_hub(hub_dir: Path, spoke: Path) -> bool:
    conn = _open(hub_dir)
    try:
        machine_id = machine_identity.get_or_create_machine_id(spoke)
        return (
            conn.execute("SELECT 1 FROM machines WHERE machine_id = ?", (machine_id,)).fetchone()
            is not None
        )
    finally:
        conn.close()


def _count(data_dir: Path, table: str) -> int:
    conn = _open(data_dir)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _hub_row_as_wire(hub_dir: Path, stable_id: str, **changes: Any) -> dict[str, Any]:
    """The hub's own stored row in wire shape, with ``changes`` applied."""
    conn = _open(hub_dir)
    try:
        columns = protocol.table_columns(conn, "tracks")
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM tracks WHERE stable_id = ?", (stable_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"{stable_id} never reached the hub"
    values = dict(protocol.canonical_row("tracks", columns, row))
    values.update(changes)
    return {"table": "tracks", "pk": [stable_id], "values": values}


def _move_client_build_version(
    monkeypatch: pytest.MonkeyPatch, schema: int, wire: int | None = None
) -> None:
    """Make every client module that reads ``SCHEMA_VERSION`` see ``schema``.

    Only the client's view moves; the DB still migrates through
    ``apps.shared.state.db``'s own reference and the in-process hub keeps its
    own. ``setattr`` without ``raising=False``, so a module that stops binding
    ``state_schema`` fails here loudly instead of silently going unpatched.

    ``wire`` defaults to the real, unmoved ``wire_version.WIRE_VERSION``: a
    schema-only skew is what X2 (#1999) made sync instead of refuse (see
    ``test_hub_sync_wire_version.py``), so leaving it unmoved keeps that
    existing invariant testable elsewhere in this file. Pass a moved ``wire``
    to simulate a genuinely wire-incompatible build, the only kind this
    codebase still refuses.
    """
    moved_schema = SimpleNamespace(SCHEMA_VERSION=schema)
    moved_wire = SimpleNamespace(
        WIRE_VERSION=wire_version.WIRE_VERSION if wire is None else wire,
        incompatibility=wire_version.incompatibility,
        CODE_WIRE=wire_version.CODE_WIRE,
        CODE_SCHEMA=wire_version.CODE_SCHEMA,
    )
    for module in (client, client_transport_ops):
        monkeypatch.setattr(module, "state_schema", moved_schema)
        monkeypatch.setattr(module, "wire_version", moved_wire)


def _assert_refused(excinfo: pytest.ExceptionInfo[client.SyncTransportError], code: str) -> None:
    message = str(excinfo.value)
    assert "HTTP 409" in message and code in message, f"expected 409 {code}, got {message}"


# ----- hello ---------------------------------------------------------------


@pytest.mark.parametrize("offset", VERSION_OFFSETS, ids=OFFSET_IDS)
def test_hello_from_another_schema_version_is_refused_and_registers_nothing(
    hub: TestClientTransport, hub_dir: Path, spoke_a: Path, offset: int
) -> None:
    """if a hello one schema version away is accepted or registers the spoke then broken"""
    print("if a hello one schema version away is accepted or registers the spoke then broken")
    with pytest.raises(client.SyncTransportError) as excinfo:
        _hello(hub, spoke_a, state_schema.SCHEMA_VERSION + offset)
    _assert_refused(excinfo, "SYNC_SCHEMA_VERSION")
    assert not _registered_on_hub(hub_dir, spoke_a), "a refused hello registered the spoke"

    # Positive control: the identical hello at the hub's version registers it.
    _hello(hub, spoke_a, state_schema.SCHEMA_VERSION)
    assert _registered_on_hub(hub_dir, spoke_a)


# ----- push ----------------------------------------------------------------


@pytest.mark.parametrize("offset", VERSION_OFFSETS, ids=OFFSET_IDS)
def test_push_from_another_schema_version_is_refused_before_any_row_lands(
    hub: TestClientTransport, hub_dir: Path, spoke_a: Path, offset: int
) -> None:
    """if a push one schema version away lands a row or a changelog entry then broken"""
    print("if a push one schema version away lands a row or a changelog entry then broken")
    _seed_common_track((spoke_a,), "trk-1")
    registered = client.run_sync(spoke_a, "http://hub.invalid", transport=hub, name="spoke-a")
    row = _hub_row_as_wire(hub_dir, "trk-1", title="from another version", updated_at=_T3)
    changelog_before = _count(hub_dir, "hub_changelog")

    def push(version: int) -> dict[str, Any]:
        return hub.post(
            PUSH_PATH,
            {"machine_id": registered.machine_id, "schema_version": version, "rows": [row]},
        )

    with pytest.raises(client.SyncTransportError) as excinfo:
        push(state_schema.SCHEMA_VERSION + offset)
    _assert_refused(excinfo, "SYNC_SCHEMA_VERSION")
    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-1") == "original", "the refused push landed a row"
    finally:
        hub_conn.close()
    assert _count(hub_dir, "hub_changelog") == changelog_before

    # Positive control: the identical row at the hub's version lands.
    assert push(state_schema.SCHEMA_VERSION)["accepted"] == 1
    assert _count(hub_dir, "hub_changelog") == changelog_before + 1


# ----- pull and digest -----------------------------------------------------


@pytest.mark.parametrize("endpoint", ["pull", "digest"])
@pytest.mark.parametrize("offset", VERSION_OFFSETS, ids=OFFSET_IDS)
def test_a_spoke_refused_at_hello_cannot_pull_or_read_the_digest(
    hub: TestClientTransport, spoke_a: Path, endpoint: str, offset: int
) -> None:
    """if a spoke refused at hello can still pull or read the digest then broken"""
    print("if a spoke refused at hello can still pull or read the digest then broken")
    machine_id = machine_identity.get_or_create_machine_id(spoke_a)
    with pytest.raises(client.SyncTransportError):
        _hello(hub, spoke_a, state_schema.SCHEMA_VERSION + offset)

    path = f"{client.API_PREFIX}/{endpoint}"
    with pytest.raises(client.SyncTransportError) as excinfo:
        hub.get(path, {"machine_id": machine_id})
    _assert_refused(excinfo, "SYNC_UNKNOWN_MACHINE")

    # Positive control: after a hello at the hub's version the same GET answers.
    _hello(hub, spoke_a, state_schema.SCHEMA_VERSION)
    assert "seq" in hub.get(path, {"machine_id": machine_id})


@pytest.mark.parametrize("endpoint", ["pull", "digest"])
@pytest.mark.parametrize("offset", VERSION_OFFSETS, ids=OFFSET_IDS)
def test_a_registered_spoke_on_another_build_version_is_still_served(
    hub: TestClientTransport,
    spoke_a: Path,
    endpoint: str,
    offset: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if pull or digest start gating a registered spoke's build version unnoticed then broken"""
    print("if pull or digest start gating a registered spoke's build version unnoticed then broken")
    _seed_common_track((spoke_a,), "trk-1")
    registered = client.run_sync(spoke_a, "http://hub.invalid", transport=hub, name="spoke-a")
    # Registered at the hub's version, then the spoke's BUILD moves. Every
    # client module that reads the version moves with it, so a version added
    # to either request later is sent at the moved value, and this goes red.
    _move_client_build_version(monkeypatch, state_schema.SCHEMA_VERSION + offset)
    recording = FaultTransport(hub)
    conn = _open(spoke_a)
    try:
        if endpoint == "pull":
            pulled = client._pull_in_chunks(
                recording, conn, registered.machine_id, 0, registered.hub_machine_id, limit=100
            ).pulled
            assert pulled >= 1, "the pull must serve the registered spoke's changelog"
        elif endpoint == "digest":
            local, remote = client._digests(recording, conn, registered.machine_id)
            assert remote.overall == local.overall, "the digest must answer for the synced spoke"
        else:
            raise AssertionError(f"unhandled endpoint {endpoint}")
    finally:
        conn.close()
    assert [(call.path, call.outcome) for call in recording.calls] == [
        (f"{client.API_PREFIX}/{endpoint}", "delivered")
    ], (
        "a registered spoke on another build version is served today, because "
        "these endpoints carry no version (private issue evidence unavailable in this copy); if this now "
        "refuses, the gap is closed and this pin should become a refusal test"
    )


# ----- the client ------------------------------------------------------------


@pytest.mark.parametrize("offset", VERSION_OFFSETS, ids=OFFSET_IDS)
def test_run_sync_one_version_away_stops_at_hello(
    hub: TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    offset: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if run_sync from a spoke on another wire version sends anything past hello then broken

    Schema-only skew, same wire, is deliberately NOT refused since X2 (#1999)
    (test_hub_sync_wire_version.py pins that as sync-and-converge, not a
    refusal), so this moves wire_version too: the invariant this test still
    owns is that a genuinely wire-incompatible build never gets past hello.
    """
    print("if run_sync from a spoke on another wire version sends anything past hello then broken")
    _seed_common_track((spoke_a,), "trk-1")
    # The spoke's BUILD claims another wire version (and schema moves with it,
    # as a real release would).
    _move_client_build_version(
        monkeypatch,
        state_schema.SCHEMA_VERSION + offset,
        wire_version.WIRE_VERSION + offset,
    )
    recording = FaultTransport(hub)
    with pytest.raises(client.SyncTransportError, match="SYNC_WIRE_VERSION"):
        client.run_sync(spoke_a, "http://hub.invalid", transport=recording, name="spoke-a")

    assert [(call.path, call.outcome) for call in recording.calls] == [(HELLO_PATH, "hub_error")]
    assert _count(hub_dir, "tracks") == 0, "a row crossed despite the version refusal"
    assert _count(spoke_a, "sync_state") == 0, "the spoke recorded a watermark for a refused sync"


@pytest.mark.parametrize("offset", VERSION_OFFSETS, ids=OFFSET_IDS)
def test_the_client_refuses_a_hub_reporting_another_version_before_any_push(
    hub: TestClientTransport, hub_dir: Path, spoke_a: Path, offset: int
) -> None:
    """if the client pushes to a hub that reports another wire version then broken

    Same reasoning as test_run_sync_one_version_away_stops_at_hello: a hub
    reporting a different schema_version alone, same wire, is not a refusal
    since X2 (#1999). Tamper wire_version too so this keeps proving the
    invariant that still holds: the client's own post-hello check must stop
    before any push against a hub on another wire.
    """
    print("if the client pushes to a hub that reports another wire version then broken")
    _seed_common_track((spoke_a,), "trk-1")
    reported_schema = state_schema.SCHEMA_VERSION + offset
    reported_wire = wire_version.WIRE_VERSION + offset
    faulty = FaultTransport(hub)
    faulty.tamper_response(
        "hello",
        lambda body: {**body, "schema_version": reported_schema, "wire_version": reported_wire},
    )

    with pytest.raises(client.SyncVersionMismatch, match=f"hub speaks sync wire v{reported_wire}"):
        client.run_sync(spoke_a, "http://hub.invalid", transport=faulty, name="spoke-a")
    faulty.assert_all_fired()

    assert [(call.path, call.outcome) for call in faulty.calls] == [(HELLO_PATH, "tampered")], (
        "the client must stop at hello; any later call means it pushed or pulled "
        "against a hub on another wire"
    )
    assert _count(hub_dir, "tracks") == 0
    assert _count(spoke_a, "sync_state") == 0
