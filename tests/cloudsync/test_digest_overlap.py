"""The spoke's digest must not wait for the hub's, or the other way round (LIBM-120 L6).

At the end of a sync the spoke hashes its whole library, then asks the hub
to hash its own. On the 10,000-track fixture each side takes about 2 s of
CPU, in two different processes, and nothing in either depends on the other:
two reads of two databases. Run one after the other they cost the sum; run
together they cost the larger.

The instrument is a rendezvous observed from OUTSIDE the code under test.
Neither ``protocol.sync_digest`` nor ``_fetch_hub_digest`` is replaced:

* the spoke side is the spoke connection's own sqlite trace callback, which
  fires as the real digest issues its first read and waits, bounded, for the
  hub request to have arrived;
* the hub side is an HTTP middleware on the test hub app, which fires as the
  real ``GET /digest`` reaches the real router and waits, bounded, for the
  spoke's digest to have started.

Sequential calls can never meet, so the spoke side times out and says so.

[if] the local digest and the hub fetch cannot meet [then] they run in series, [else stop].

Every test here asserts the meeting, so each goes red when ``_digests`` runs
the two in series again. The controls also assert, each against its own
overshoot:

* the local digest is still taken inside one read transaction (ADR 08 6b);
* the ``digest`` phase still times the hub fetch alone, not the local hash;
* a real hub refusal, and a real local database error, each propagate as
  themselves, and leave no worker thread behind; the failing local digest
  also waits for the in-flight hub request instead of abandoning it.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import client, protocol, service
from apps.sync_hub.protocol_common import MEMBERSHIP_TABLE
from apps.sync_hub.sync_timing import PhaseTimer
from apps.sync_hub.transport import SyncTransportError
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_track_identity_lookup_scale import _seed_library

pytestmark = pytest.mark.requirement("LIBM-120")

MEET_TIMEOUT_S = 10.0
# Wide enough that a hub fetch on a loaded CI host (it took over 0.5 s at load
# about 270 on silver) still reads under half of it; a span that also covers
# the local hash reads at least this much.
SLOW_LOCAL_S = 3.0
SMALL_LIBRARY = 10
LARGE_LIBRARY = 200
TRANSACTION_CONTROL = ("BEGIN", "COMMIT", "ROLLBACK")


# ----- the external probe -------------------------------------------------------


@dataclass
class _DigestProbe:
    """What the spoke connection and the hub router each saw, and when.

    ``local_met`` / ``hub_met`` are None until that side has waited for the
    other, then say whether the other side had started within the bound.
    ``hold_hub_until_rollback`` keeps the hub request in flight until the
    spoke's read transaction has rolled back, so a failing local digest is
    guaranteed to fail while the hub fetch is still running.
    """

    hold_hub_until_rollback: bool = False
    slow_local_s: float = 0.0
    local_started: threading.Event = field(default_factory=threading.Event)
    hub_started: threading.Event = field(default_factory=threading.Event)
    hub_finished: threading.Event = field(default_factory=threading.Event)
    spoke_rolled_back: threading.Event = field(default_factory=threading.Event)
    local_met: bool | None = None
    hub_met: bool | None = None
    hub_status: int | None = None
    reads_in_transaction: list[bool] = field(default_factory=list)

    @property
    def met(self) -> bool:
        return self.local_met is True and self.hub_met is True

    def watch_spoke(self, conn: sqlite3.Connection) -> None:
        def on_statement(sql: str) -> None:
            verb = sql.lstrip().split(None, 1)[0].upper()
            if verb == "ROLLBACK":
                self.spoke_rolled_back.set()
            if verb in TRANSACTION_CONTROL:
                return
            self.reads_in_transaction.append(conn.in_transaction)
            if self.local_started.is_set():
                return
            self.local_started.set()
            self.local_met = self.hub_started.wait(MEET_TIMEOUT_S)
            time.sleep(self.slow_local_s)

        conn.set_trace_callback(on_statement)

    async def around_hub_digest(self) -> None:
        self.hub_started.set()
        self.hub_met = await asyncio.to_thread(self.local_started.wait, MEET_TIMEOUT_S)
        if self.hold_hub_until_rollback:
            await asyncio.to_thread(self.spoke_rolled_back.wait, MEET_TIMEOUT_S)


def _assert_met(probe: _DigestProbe) -> None:
    assert probe.local_met is True, (
        "the local digest waited for the hub's GET /digest and it never arrived: "
        "the two digests run in series (LIBM-120 L6)"
    )
    assert probe.hub_met is True, (
        "the hub's GET /digest waited for the local digest and it never started: "
        "the two digests run in series (LIBM-120 L6)"
    )


# ----- fixtures -----------------------------------------------------------------


@pytest.fixture
def hub_app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(tmp_path / "hub"))
    app.state.sync_hub_data_dir = str(tmp_path / "hub")
    app.state.sync_hub_machine_name = "hub"
    app.state.digest_probe = None
    app.include_router(service.router, prefix="/api/v1")

    @app.middleware("http")
    async def observe_digest(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        probe: _DigestProbe | None = request.app.state.digest_probe
        if probe is None or not request.url.path.endswith("/digest"):
            return await call_next(request)
        await probe.around_hub_digest()
        response = await call_next(request)
        probe.hub_status = response.status_code
        probe.hub_finished.set()
        return response

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


def _armed(hub: FastAPI, conn: sqlite3.Connection, probe: _DigestProbe) -> _DigestProbe:
    """Arm the probe on both sides only once the setup sync has finished."""
    probe.watch_spoke(conn)
    hub.state.digest_probe = probe
    return probe


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
        conn.set_trace_callback(None)
        conn.close()


# ----- the finding --------------------------------------------------------------


@pytest.mark.parametrize("tracks", [SMALL_LIBRARY, LARGE_LIBRARY])
def test_the_two_digests_run_at_the_same_time(
    hub_app: FastAPI,
    tmp_path: Path,
    spoke_conn: Callable[[Path], sqlite3.Connection],
    tracks: int,
) -> None:
    print("if the local digest waits for the hub digest, or the hub for the local, then broken")
    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, tracks)
        conn = spoke_conn(spoke)
        probe = _armed(hub_app, conn, _DigestProbe())
        local, remote = client._digests(TestClientTransport(http), conn, machine_id)
    _assert_met(probe)
    assert probe.hub_status == 200
    assert local.overall == remote.overall, "both digests must describe the synced library"


# ----- overshoot controls -------------------------------------------------------


def test_the_local_digest_is_still_one_read_snapshot(
    hub_app: FastAPI,
    tmp_path: Path,
    spoke_conn: Callable[[Path], sqlite3.Connection],
) -> None:
    print("if the local digest leaves its read transaction to overlap the hub, then broken")
    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, SMALL_LIBRARY)
        conn = spoke_conn(spoke)
        probe = _armed(hub_app, conn, _DigestProbe())
        client._digests(TestClientTransport(http), conn, machine_id)
    _assert_met(probe)
    assert probe.reads_in_transaction, "the probe saw no local digest read at all"
    assert all(probe.reads_in_transaction), (
        "the local digest ran a read outside a read transaction, so it can describe a "
        "state that never existed (ADR 08 point 6b)"
    )


def test_the_digest_phase_times_the_hub_fetch_alone(
    hub_app: FastAPI,
    tmp_path: Path,
    spoke_conn: Callable[[Path], sqlite3.Connection],
) -> None:
    print("if cloudsync_digest_s counts the local hash as well as the hub fetch, then broken")
    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, SMALL_LIBRARY)
        conn = spoke_conn(spoke)
        probe = _armed(hub_app, conn, _DigestProbe(slow_local_s=SLOW_LOCAL_S))
        timer = PhaseTimer()
        client._digests(TestClientTransport(http), conn, machine_id, timer=timer)
    _assert_met(probe)
    digest_s = timer._phase_s["digest"]
    assert 0 < digest_s < SLOW_LOCAL_S / 2, (
        f"digest phase {digest_s:.3f} s against a {SLOW_LOCAL_S} s local hash: the "
        "span must cover the hub fetch only"
    )


def test_a_refused_hub_digest_propagates_and_leaves_no_worker(
    hub_app: FastAPI,
    tmp_path: Path,
    spoke_conn: Callable[[Path], sqlite3.Connection],
) -> None:
    print("if a refused hub digest is swallowed or leaves its worker running, then broken")
    with TestClient(hub_app) as http:
        spoke, _machine_id = _synced_spoke(tmp_path, http, SMALL_LIBRARY)
        conn = spoke_conn(spoke)
        probe = _armed(hub_app, conn, _DigestProbe())
        # A machine id the hub never registered: the real router refuses it.
        with pytest.raises(SyncTransportError, match="GET /api/v1/sync/digest") as refused:
            client._digests(TestClientTransport(http), conn, "never-registered-machine")
    _assert_met(probe)
    assert refused.value.status_code == probe.hub_status
    assert probe.hub_status is not None and 400 <= probe.hub_status < 500
    assert _digest_workers() == []
    assert conn.in_transaction is False, "the local read transaction must still be closed"


def test_a_failing_local_digest_waits_for_the_hub_fetch_and_leaves_no_worker(
    hub_app: FastAPI,
    tmp_path: Path,
    spoke_conn: Callable[[Path], sqlite3.Connection],
) -> None:
    print("if a failed local digest is swallowed or leaves the hub fetch running, then broken")
    with TestClient(hub_app) as http:
        spoke, machine_id = _synced_spoke(tmp_path, http, SMALL_LIBRARY)
        conn = spoke_conn(spoke)
        # A real damaged spoke database: the last table the digest walks is gone.
        conn.execute(f"DROP TABLE {MEMBERSHIP_TABLE}")
        probe = _armed(hub_app, conn, _DigestProbe(hold_hub_until_rollback=True))
        with pytest.raises(
            protocol.SyncProtocolError, match=f"'{MEMBERSHIP_TABLE}' does not exist"
        ):
            client._digests(TestClientTransport(http), conn, machine_id)
        assert probe.hub_finished.is_set(), (
            "the in-flight hub fetch must be joined before the local error leaves"
        )
    _assert_met(probe)
    assert probe.spoke_rolled_back.is_set(), "the local read transaction must be rolled back"
    assert _digest_workers() == []
    assert conn.in_transaction is False
