"""Round 4 hardening: the round 3 adversarial findings this lane fixed.

Companion to :mod:`tests.cloudsync.test_hub_sync_hardening` (round 1) and
:mod:`tests.cloudsync.test_hub_sync_round3` (round 2). Each test here is one
reproduction from ``.planning/cloudsync-round3-adversarial.md`` Part 2, for the
engine-security-restore lane (R1, R2, R6, R7). The mapping, so a failure names
the defect it just let back in:

- R1  -> the ``machines`` snapshot merged on push/pull was a write primitive
  over every OTHER machine's registry row, gated only by a wall clock. A peer
  could rename a victim, flip its ``is_hub``, rewrite its ``data_root``; the
  poison travelled back and the victim re-applied it on every sync. R1a: a
  forged ``name`` collision could raise out of a THIRD machine's ``hello`` and
  lock it out.
- R2  -> replacing the MAX(seq) restore heuristic with a generation token
  removed the ONE branch that recovered a hub whose DATA DIR was rolled back
  with its DB (a whole-machine restore). The token then agreed with the DB,
  nothing detected it, and the spoke halted on ``SyncDigestMismatch`` forever.
- R6  -> the settling round settled exactly once, so a fleet writing across
  two round trips fired the corruption alarm on ordinary concurrency.
- R7  -> nothing outside pytest ever called ``run_sync``; the spoke had no
  operator or agent entry point, and its engine-side failures were undeclared.

Acceptance criteria, one test each:
- if a peer's snapshot rewrites another machine's name/platform/is_hub/data_root,
  or the victim re-poisons itself on sync, the owner check is gone -- broken.
- if a forged name collision in a snapshot raises out of hello/push, the
  locked-out machine is a bystander -- broken (R1a).
- if a whole-data-dir restore halts on SyncDigestMismatch instead of
  recovering, the belt that re-offers under an unchanged generation is gone --
  broken.
- if a fleet writing across two round trips raises rather than settling, the
  bound is one; if a fleet that never settles raises the corruption alarm
  rather than a distinct error, "concurrent" and "corrupt" share a message --
  broken.
- if the CLI cannot drive one sync end to end, the spoke has no caller --
  broken.
- if an unorderable stored stamp escapes run_sync as an undeclared type, the
  engine-side raise contract is not declared -- broken.
"""
from __future__ import annotations

import json
import socket
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import normalize_stamps
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, engine, generation, maintenance, protocol, service
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T0,
    _T1,
    _T2,
    _insert_track,
    _open,
    _seed_common_track,
    _sync,
    _TestClientTransport,
    _track_title,
)
from tests.waits import start_uvicorn_in_thread

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


def _make_hub_app(hub_dir: Path) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    return app


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
    with TestClient(_make_hub_app(hub_dir)) as http:
        yield _TestClientTransport(http)


def _machine_row(conn: sqlite3.Connection, machine_id: str) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT name, platform, is_hub, data_root FROM machines WHERE machine_id = ?",
        (machine_id,),
    ).fetchone()


# ----- R1: the machines snapshot is no longer a write primitive -------------


def test_a_forged_snapshot_cannot_rewrite_a_peers_registry_row(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """R1's c1 probe: a peer forges A's row; it must not stick, and A must not
    re-poison itself.

    Round 3 observed A's hub row rewritten to
    ``('pwned', 'windows', 1, '/attacker/root', '2099-...')`` by B's push, and
    -- the line that matters -- A's OWN row rewritten to the same forged values
    after A merged the hello snapshot. The owner-scoped merge (update only the
    caller's row, insert-or-ignore everyone else) closes both.
    """
    a = _sync(spoke_a, hub, "spoke-a")
    b = _sync(spoke_b, hub, "spoke-b")

    hub_conn = _open(hub_dir)
    try:
        before = _machine_row(hub_conn, a.machine_id)
    finally:
        hub_conn.close()
    assert before is not None and before[0] == "spoke-a"

    # B forges A's registry row inside its push snapshot, with a wall clock
    # far in the future -- the value that outranked ``now`` under the old gate.
    forged = {
        "machine_id": a.machine_id,
        "name": "pwned",
        "platform": "windows",
        "is_hub": True,
        "data_root": "/attacker/root",
        "first_seen": _T0,
        "last_seen": "2099-01-01T00:00:00.000000+00:00",
    }
    accepted = hub.post(
        f"{client.API_PREFIX}/push",
        {
            "machine_id": b.machine_id,
            "schema_version": state_schema.SCHEMA_VERSION,
            "rows": [],
            "machines": [forged],
        },
    )
    assert accepted["accepted"] == 0

    hub_conn = _open(hub_dir)
    try:
        assert _machine_row(hub_conn, a.machine_id) == before, (
            "a peer rewrote another machine's registry row"
        )
    finally:
        hub_conn.close()

    # A syncs again. The hello snapshot it merges must not overwrite A's own
    # row (it was clean here, but the merge is what re-poisoned it in round 3).
    _sync(spoke_a, hub, "spoke-a")
    hub_conn = _open(hub_dir)
    try:
        assert _machine_row(hub_conn, a.machine_id) == before
    finally:
        hub_conn.close()
    conn_a = _open(spoke_a)
    try:
        local = _machine_row(conn_a, a.machine_id)
    finally:
        conn_a.close()
    assert local is not None
    assert local[0] == "spoke-a" and local[1] != "windows", (
        "the victim re-poisoned its own registry row on sync"
    )


def test_a_forged_name_collision_in_a_snapshot_does_not_lock_out_a_bystander(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """R1a: a snapshot forging a name a THIRD machine holds must not raise.

    Round 3 observed ``SyncApplyError: ... UNIQUE constraint failed:
    machines.name`` escaping the merge, so the machine the forged name
    collided with could not sync at all. ``INSERT OR IGNORE`` swallows the
    collision: the ghost is simply not created and the victim keeps its name.
    """
    a = _sync(spoke_a, hub, "spoke-a")
    b = _sync(spoke_b, hub, "spoke-b")

    ghost = "c" * 32
    forged = {
        "machine_id": ghost,
        "name": "spoke-a",  # the name A already holds
        "platform": "linux",
        "is_hub": False,
        "data_root": None,
        "first_seen": _T0,
        "last_seen": "2099-01-01T00:00:00.000000+00:00",
    }
    # Must NOT raise out of push.
    accepted = hub.post(
        f"{client.API_PREFIX}/push",
        {
            "machine_id": b.machine_id,
            "schema_version": state_schema.SCHEMA_VERSION,
            "rows": [],
            "machines": [forged],
        },
    )
    assert accepted["accepted"] == 0

    hub_conn = _open(hub_dir)
    try:
        assert hub_conn.execute(
            "SELECT COUNT(*) FROM machines WHERE machine_id = ?", (ghost,)
        ).fetchone() == (0,), "the ghost with a colliding name was inserted"
        assert hub_conn.execute(
            "SELECT machine_id FROM machines WHERE name = 'spoke-a'"
        ).fetchone() == (a.machine_id,), "the victim lost its name to the forgery"
    finally:
        hub_conn.close()

    # And the victim is not locked out: it still syncs.
    again = _sync(spoke_a, hub, "spoke-a")
    assert again.machine_id == a.machine_id


# ----- R2: a whole-data-dir restore recovers rather than bricking -----------


def test_a_whole_data_dir_restore_recovers_instead_of_bricking(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """R2's c3-whole probe: the generation is UNCHANGED, so only the belt saves it.

    A Litestream DB restore rotates the token and is already handled. A restore
    that rolled the DATA DIR back with the DB leaves the anchor agreeing with
    the DB, so nothing detects it. Round 3 lowered only the pull floor and the
    spoke halted on ``SyncDigestMismatch`` on every retry, forever. The belt
    resets BOTH floors when the hub's seq falls below this spoke's pull floor
    under an unchanged generation, and re-offers.
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

    token_before = maintenance.show_generation(hub_dir)

    # Roll the hub's DB back to before trk-2 landed, dropping its changelog
    # entry so MAX(seq) actually falls below what the spoke pulled.
    hub_conn = _open(hub_dir)
    try:
        gone_seq = hub_conn.execute(
            "SELECT seq FROM hub_changelog WHERE table_name = 'tracks' AND row_pk = ?",
            (protocol.encode_row_pk(("trk-2",)),),
        ).fetchone()[0]
        hub_conn.execute("DELETE FROM tracks WHERE stable_id = 'trk-2'")
        hub_conn.execute("DELETE FROM hub_changelog WHERE seq = ?", (gone_seq,))
        hub_conn.commit()
        rolled_seq = engine.current_seq(hub_conn)
    finally:
        hub_conn.close()
    assert rolled_seq == 1, "the rollback must leave the hub below the spoke's pull floor"

    # Roll the anchor back WITH the DB (same token, matching high-water), which
    # is exactly what a whole-machine restore does -- so nothing rotates.
    generation.anchor_path(hub_dir).write_text(
        json.dumps(
            {"generation": token_before, "high_water": rolled_seq}, sort_keys=True
        ),
        encoding="utf-8",
    )

    recovered = _sync(spoke_a, hub, "spoke-a")
    assert recovered.hub_restore_detected is True, "the seq-regression belt did not fire"
    assert maintenance.show_generation(hub_dir) == token_before, (
        "the token rotated; this was supposed to be the case the token cannot see"
    )

    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-1") == "one"
        assert _track_title(hub_conn, "trk-2") == "two", (
            "the row the hub forgot was never re-offered; the hub is bricked"
        )
    finally:
        hub_conn.close()

    # And it settles clean afterwards, with no repeated restore signal.
    quiet = _sync(spoke_a, hub, "spoke-a")
    assert quiet.hub_restore_detected is False


# ----- R6: the settle loop is bounded, and has a distinct over-bound error --


class _RepeatingIntercept:
    """Fire ``during(n)`` before the first ``times`` requests to ``before``.

    Round 3's ``_InterceptingTransport`` fires once; the settle bound needs a
    write landing in several consecutive round trips to prove the loop keeps
    settling AND stops. Nothing about the protocol is faked: the wrapped
    transport is the real one and the callback drives real writes.
    """

    def __init__(
        self,
        inner: _TestClientTransport,
        *,
        before: str,
        during: Callable[[int], None],
        times: int,
    ) -> None:
        self._inner = inner
        self._before = before
        self._during = during
        self._times = times
        self.fires = 0

    def _maybe_fire(self, path: str) -> None:
        if self.fires >= self._times or not path.endswith(self._before):
            return
        self.fires += 1
        self._during(self.fires)

    def post(self, path: str, payload: Any) -> dict[str, Any]:
        self._maybe_fire(path)
        return self._inner.post(path, payload)

    def get(self, path: str, params: Any) -> dict[str, Any]:
        self._maybe_fire(path)
        return self._inner.get(path, params)


def test_a_fleet_writing_across_two_round_trips_still_settles(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """R6: a write in each of two round trips converges on the third round.

    Round 3 settled exactly once, so this raised ``SyncDigestMismatch`` -- the
    corruption alarm -- on ordinary concurrency. The loop settles until the
    fence stops moving, bounded at :data:`client.MAX_ROUNDS`.
    """
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")

    def _write(n: int) -> None:
        conn = _open(spoke_a)
        try:
            _insert_track(
                conn, f"trk-mid-{n}", title=f"mid {n}", updated_at=_T2, origin=_DEV_A
            )
        finally:
            conn.close()

    intercepted = _RepeatingIntercept(hub, before="/pull", during=_write, times=2)
    result = client.run_sync(
        spoke_a, "http://hub.invalid", transport=intercepted, name="spoke-a"
    )

    assert intercepted.fires == 2, "the probe did not land a write in each round trip"
    assert result.rounds == 3, "the sync did not settle across two settle rounds"
    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-mid-1") == "mid 1"
        assert _track_title(hub_conn, "trk-mid-2") == "mid 2"
    finally:
        hub_conn.close()


def test_a_fleet_that_never_settles_raises_still_moving_not_mismatch(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """R6: past the bound, a still-moving fence is its OWN error.

    "Concurrent writing" and "corrupt merge" must never share a message, or
    the one alarm ADR 04 c6 reserves for corruption stops being trustworthy.
    """
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")

    def _write(n: int) -> None:
        conn = _open(spoke_a)
        try:
            _insert_track(
                conn, f"trk-mid-{n}", title=f"mid {n}", updated_at=_T2, origin=_DEV_A
            )
        finally:
            conn.close()

    intercepted = _RepeatingIntercept(hub, before="/pull", during=_write, times=99)
    with pytest.raises(client.SyncStillMoving) as excinfo:
        client.run_sync(
            spoke_a, "http://hub.invalid", transport=intercepted, name="spoke-a"
        )

    assert "concurrent" in str(excinfo.value).lower()
    assert not isinstance(excinfo.value, client.SyncDigestMismatch), (
        "the still-moving error is a SyncDigestMismatch; the alarm is muddled"
    )
    assert intercepted.fires >= client.MAX_ROUNDS


# ----- R7: the spoke has a caller (CLI), and declared engine-side raises -----


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def live_hub(hub_dir: Path) -> Iterator[str]:
    """A real HTTP hub on the loopback, so the CLI talks real bytes, not a
    ``TestClient``. No mock: the production router behind uvicorn."""
    port = _free_port()
    config = uvicorn.Config(
        _make_hub_app(hub_dir), host="127.0.0.1", port=port, log_level="warning"
    )
    server, thread = start_uvicorn_in_thread(config, what="the live hub")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_the_cli_syncs_end_to_end_against_a_live_hub(
    live_hub: str,
    hub_dir: Path,
    spoke_a: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R7: ``python -m apps.sync_hub sync`` drives one round trip over HTTP.

    Nothing outside pytest called ``run_sync`` before; the spoke had no
    operator or agent entry point (agent-native parity). This drives the CLI's
    ``main`` against a real server end to end.
    """
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
    finally:
        conn_a.close()

    exit_code = maintenance.main(
        ["sync", "--data-dir", str(spoke_a), "--hub", live_hub, "--name", "spoke-a"]
    )
    assert exit_code == 0
    assert "synced against hub" in capsys.readouterr().out

    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-1") == "one", (
            "the CLI sync did not reach the hub"
        )
    finally:
        hub_conn.close()


def test_run_sync_quarantines_one_unorderable_stamp_and_syncs_the_rest(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """R7 / R4's engine-side raise-contract half, as round 5 settled it.

    Round 4 aborted ``spoke_push`` on an unorderable stored ``updated_at``
    before anything reached the wire, so one legacy row stopped every push
    and every digest on the machine, forever. It now quarantines THAT ROW:
    the row is not offered, the count says so, the digest compares the
    eligible set, and the sync completes. The declared-type contract is
    unchanged for the failures that remain.
    """
    assert client.SyncProtocolError is protocol.SyncProtocolError
    assert client.SyncApplyError is engine.SyncApplyError

    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
        _insert_track(conn_a, "trk-2", title="two", updated_at=_T0, origin=_DEV_A)
        # A naive, space-separated stamp: not orderable, stored raw past the
        # writer so canonical_row would hit it on the next full offer.
        conn_a.execute(
            "UPDATE tracks SET updated_at = '2026-08-30 10:00:00' "
            "WHERE stable_id = 'trk-1'"
        )
        conn_a.commit()
    finally:
        conn_a.close()

    result = _sync(spoke_a, hub, "spoke-a")
    assert result.quarantined == 1, "the poisoned row must be counted, not silent"

    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-2") == "two", (
            "one legacy row must not stop an innocent row from syncing"
        )
        assert hub_conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE stable_id = 'trk-1'"
        ).fetchone()[0] == 0, (
            "a quarantined row must not reach the peer at all -- under the "
            "coalescing design it would arrive stamped year zero"
        )
    finally:
        hub_conn.close()

    # Control: after the SANCTIONED repair the same row DOES sync and nothing
    # is quarantined. Without this the assertions above would pass for a
    # spoke that had simply stopped pushing anything. Driven through
    # normalize_stamps rather than a raw UPDATE on purpose: a bare UPDATE
    # leaves no local_changelog entry, so the fenced push would never offer
    # the row and the control would fail for a reason that is not the one
    # under test.
    conn_a = _open(spoke_a)
    try:
        repairs = normalize_stamps.scan(conn_a)
        assert [(r.table, r.column) for r in repairs] == [("tracks", "updated_at")]
        normalize_stamps.apply_repairs(conn_a, repairs)
    finally:
        conn_a.close()
    healed = _sync(spoke_a, hub, "spoke-a")
    assert healed.quarantined == 0
    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-1") == "one"
    finally:
        hub_conn.close()
