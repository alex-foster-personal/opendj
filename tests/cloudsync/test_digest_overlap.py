"""The spoke's digest must not wait for the hub's, or the other way round (LIBM-120 L6).

At the end of a sync the spoke hashes its whole library, then asks the hub
to hash its own. On the 10,000-track fixture each side takes about 2 s of
CPU, in two different processes, and nothing in either depends on the other:
two reads of two databases. Run one after the other they cost the sum; run
together they cost the larger.

The instrument is a rendezvous: the local digest and the hub's ``GET
/digest`` each wait, bounded, for the other to have started. Sequential
calls can never meet, so the probe times out and says which side waited.

[if] the local digest and the hub fetch cannot meet [then] they run in series, [else stop].

Controls:
* the same rendezvous must time out when ``_digests`` is made sequential again;
* the local digest must still be taken inside one read transaction (ADR 08 6b);
* the ``digest`` phase must still time the hub fetch alone, not the local hash;
* a failing hub fetch, and a failing local digest, must each propagate as
  themselves, and leave no worker thread behind.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import client, client_transport_ops, protocol, service
from apps.sync_hub.sync_timing import PhaseTimer
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_track_identity_lookup_scale import _seed_library

pytestmark = pytest.mark.requirement("LIBM-120")

MEET_TIMEOUT_S = 10.0
SERIES_TIMEOUT_S = 0.5
SLOW_HUB_S = 0.3
SMALL_LIBRARY = 10
LARGE_LIBRARY = 200


# ----- fixtures -----------------------------------------------------------------


@pytest.fixture
def hub_app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(tmp_path / "hub"))
    app.state.sync_hub_data_dir = str(tmp_path / "hub")
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    return app


def _synced_spoke(tmp_path: Path, http: TestClient, tracks: int) -> tuple[Path, str]:
    spoke = tmp_path / f"spoke-{tracks}"
    conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _seed_library(conn, tracks)
    finally:
        conn.close()
    first = client.run_sync(
        spoke, "http://hub.invalid", transport=TestClientTransport(http), name=f"spoke-{tracks}"
    )
    return spoke, first.machine_id


class _Rendezvous:
    """Two sides that each record their start, then wait for the other's.

    Only the SPOKE's digest is the local side: the in-process hub calls the
    same ``protocol.sync_digest`` for its own answer, and counting that call
    would let a hub-first series meet itself.
    """

    def __init__(self, spoke: sqlite3.Connection, timeout_s: float = MEET_TIMEOUT_S) -> None:
        self.spoke = spoke
        self.timeout_s = timeout_s
        self.local_started = threading.Event()
        self.hub_started = threading.Event()
        self.local_in_transaction: bool | None = None

    def local(
        self, inner: Callable[..., protocol.SyncDigest]
    ) -> Callable[..., protocol.SyncDigest]:
        def wrapped(conn: sqlite3.Connection, **kwargs: Any) -> protocol.SyncDigest:
            if conn is not self.spoke:
                return inner(conn, **kwargs)
            self.local_in_transaction = conn.in_transaction
            self.local_started.set()
            if not self.hub_started.wait(self.timeout_s):
                raise AssertionError(
                    "the local digest waited for the hub fetch and it never started: "
                    "the two digests run in series (LIBM-120 L6)"
                )
            return inner(conn, **kwargs)

        return wrapped

    def hub(self, inner: Callable[..., protocol.SyncDigest]) -> Callable[..., protocol.SyncDigest]:
        def wrapped(*args: Any) -> protocol.SyncDigest:
            self.hub_started.set()
            if not self.local_started.wait(self.timeout_s):
                raise AssertionError(
                    "the hub fetch waited for the local digest and it never started: "
                    "the two digests run in series (LIBM-120 L6)"
                )
            return inner(*args)

        return wrapped


def _install(monkeypatch: pytest.MonkeyPatch, meet: _Rendezvous) -> None:
    monkeypatch.setattr(client.protocol, "sync_digest", meet.local(protocol.sync_digest))
    monkeypatch.setattr(
        client, "_fetch_hub_digest", meet.hub(client_transport_ops._fetch_hub_digest)
    )


def _digest_workers() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith(client.DIGEST_WORKER_PREFIX)]


@pytest.fixture
def spoke_conn() -> Iterator[Callable[[Path], sqlite3.Connection]]:
    opened: list[sqlite3.Connection] = []

    def _open(spoke: Path) -> sqlite3.Connection:
        conn = state_db.open_rw(client.state_db_path(spoke))
        opened.append(conn)
        return conn

    yield _open
    for conn in opened:
        conn.close()


# ----- the finding --------------------------------------------------------------


@pytest.mark.parametrize("tracks", [SMALL_LIBRARY, LARGE_LIBRARY])
def test_the_two_digests_run_at_the_same_time(
    hub_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spoke_conn: Callable[[Path], sqlite3.Connection],
    tracks: int,
) -> None:
    print("if the local digest waits for the hub digest, or the hub for the local, then broken")
    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, tracks)
        conn = spoke_conn(spoke)
        meet = _Rendezvous(conn)
        _install(monkeypatch, meet)
        local, remote = client._digests(TestClientTransport(http), conn, machine_id)
    assert meet.local_started.is_set() and meet.hub_started.is_set()
    assert local.overall == remote.overall, "both digests must describe the synced library"


def test_rendezvous_times_out_when_the_digests_run_in_series(
    hub_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spoke_conn: Callable[[Path], sqlite3.Connection],
) -> None:
    """Negative control: the pre-change order, local then hub, must never meet."""

    def in_series(
        channel: Any, conn: sqlite3.Connection, machine_id: str, *, timer: PhaseTimer | None = None
    ) -> tuple[protocol.SyncDigest, protocol.SyncDigest]:
        with client_transport_ops._transaction(conn):
            local = client.protocol.sync_digest(conn)
        return local, client._fetch_hub_digest(channel, machine_id)

    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, SMALL_LIBRARY)
        conn = spoke_conn(spoke)
        meet = _Rendezvous(conn, timeout_s=SERIES_TIMEOUT_S)
        _install(monkeypatch, meet)
        with pytest.raises(AssertionError, match="run in series"):
            in_series(TestClientTransport(http), conn, machine_id)
    assert meet.local_started.is_set() and not meet.hub_started.is_set()


# ----- overshoot controls -------------------------------------------------------


def test_the_local_digest_is_still_one_read_snapshot(
    hub_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spoke_conn: Callable[[Path], sqlite3.Connection],
) -> None:
    print("if the local digest leaves its read transaction to overlap the hub, then broken")
    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, SMALL_LIBRARY)
        conn = spoke_conn(spoke)
        meet = _Rendezvous(conn)
        _install(monkeypatch, meet)
        client._digests(TestClientTransport(http), conn, machine_id)
    assert meet.local_in_transaction is True, (
        "the local digest ran outside a read transaction, so it can describe a "
        "state that never existed (ADR 08 point 6b)"
    )


def test_the_digest_phase_times_the_hub_fetch_alone(
    hub_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spoke_conn: Callable[[Path], sqlite3.Connection],
) -> None:
    print("if cloudsync_digest_s counts the local hash as well as the hub fetch, then broken")
    local_s = 1.0
    real_local = protocol.sync_digest

    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, SMALL_LIBRARY)
        conn = spoke_conn(spoke)

        def slow_local(on: sqlite3.Connection, **kwargs: Any) -> protocol.SyncDigest:
            if on is conn:
                time.sleep(local_s)
            return real_local(on, **kwargs)

        monkeypatch.setattr(client.protocol, "sync_digest", slow_local)
        timer = PhaseTimer()
        client._digests(TestClientTransport(http), conn, machine_id, timer=timer)
    digest_s = timer._phase_s["digest"]
    assert 0 < digest_s < local_s / 2, (
        f"digest phase {digest_s:.3f} s against a {local_s} s local hash: the "
        "span must cover the hub fetch only"
    )


def test_a_failing_hub_fetch_propagates_and_leaves_no_worker(
    hub_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spoke_conn: Callable[[Path], sqlite3.Connection],
) -> None:
    print("if a failed hub digest is swallowed or leaves its worker running, then broken")

    class HubDown(RuntimeError):
        pass

    def failing_fetch(*_args: Any) -> protocol.SyncDigest:
        raise HubDown("hub digest failed")

    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, SMALL_LIBRARY)
        monkeypatch.setattr(client, "_fetch_hub_digest", failing_fetch)
        with pytest.raises(HubDown, match="hub digest failed"):
            client._digests(TestClientTransport(http), spoke_conn(spoke), machine_id)
    assert _digest_workers() == []


def test_a_failing_local_digest_propagates_and_leaves_no_worker(
    hub_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spoke_conn: Callable[[Path], sqlite3.Connection],
) -> None:
    print("if a failed local digest is swallowed or leaves the hub fetch running, then broken")

    class LocalBroken(RuntimeError):
        pass

    real_local = protocol.sync_digest
    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, SMALL_LIBRARY)
        conn = spoke_conn(spoke)

        def failing_local(on: sqlite3.Connection, **kwargs: Any) -> protocol.SyncDigest:
            if on is conn:
                raise LocalBroken("local digest failed")
            return real_local(on, **kwargs)

        fetched = threading.Event()
        real_fetch = client_transport_ops._fetch_hub_digest

        def slow_fetch(*args: Any) -> protocol.SyncDigest:
            time.sleep(SLOW_HUB_S)
            answer = real_fetch(*args)
            fetched.set()
            return answer

        monkeypatch.setattr(client.protocol, "sync_digest", failing_local)
        monkeypatch.setattr(client, "_fetch_hub_digest", slow_fetch)
        with pytest.raises(LocalBroken, match="local digest failed"):
            client._digests(TestClientTransport(http), conn, machine_id)
        assert fetched.is_set(), "the in-flight hub fetch must be joined before the error leaves"
    assert _digest_workers() == []
    assert conn.in_transaction is False, "the read transaction must be rolled back"
