"""Two machines racing to redeem ONE enrollment grant (plan W8 item 2).

[if] two racing redemptions of one grant both succeed [then] fail, [else stop].

Round 4 covered replay and single use per machine, both sequentially. What it
did not cover is the leaked-grant race: two machines presenting the same
token at the same instant. ``_redeem_grant`` reads the row, decides, and
then writes it; nothing but the caller's locking stopped a second reader
from deciding on the same unspent row.

Two tiers, answering different questions:

* The FUNCTION tier stages the interleaving deterministically. Each thread
  holds its own autocommit connection, and a SQLite trace callback parks
  both at a barrier just before their ``UPDATE enrollment_grants``, so both
  have already read the grant as unspent. That is the exact window a
  SELECT-then-UPDATE leaves open, and it is reachable by any caller that
  does not hold the write lock before it reads. Before the fix, both threads
  resolved an owner and the second silently overwrote the first's
  ``redeemed_machine_id``.
* The HTTP tier races two real requests at the live uvicorn hub, several
  rounds over. It passed before the fix too, and the reason is incidental,
  not designed: the enroll route writes the hub's own ``machines`` heartbeat
  before it reads the grant, so the write lock is already held and SQLite
  serializes the two requests. The test pins the route contract anyway, so a
  refactor that reorders those writes cannot quietly reopen the race. The
  contract is exactly one 200, the other a declared 401, and never a 500.

Acceptance, one test each:
- if two machines racing on one grant can both resolve an owner then broken
- if the loser of a live race on one grant gets anything but a declared 401 then broken
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, enrollment, enrollment_credentials
from apps.sync_hub.transport import HttpTransport

from .conftest import ENROLL_OWNER_SUB, ENROLL_PATH
from .enrollment_helpers import machine_payload, mint_grant, owner_rows, read_hub

pytestmark = pytest.mark.requirement("CAT-04")

#: Long enough that a loaded CI runner still meets the barrier; short enough
#: that a thread which never reaches it fails the test instead of hanging it.
BARRIER_TIMEOUT_S: float = 10.0

#: Live-race repetitions. Each round races a FRESH grant between two fresh
#: machines; the contract has to hold on every round, not on average.
LIVE_RACE_ROUNDS: int = 8

_GRANT_UPDATE_PREFIX: str = f"UPDATE {enrollment_credentials.GRANT_TABLE}".upper()


def _grant_row(hub_dir: Path, token: str) -> dict[str, object]:
    digest = enrollment_credentials.hash_grant_token(token)
    rows = [
        row
        for row in read_hub(hub_dir, enrollment_credentials.GRANT_TABLE)
        if row["grant_token_sha256"] == digest
    ]
    assert len(rows) == 1, f"expected one grant row for the minted token, found {len(rows)}"
    return rows[0]


def _redeem_behind_barrier(
    hub_db: Path,
    token: str,
    machine_id: str,
    barrier: threading.Barrier,
    arrivals: list[str],
    barrier_errors: list[BaseException],
    outcomes: dict[str, object],
) -> None:
    """Redeem ``token`` for ``machine_id``, parked at the barrier before the write.

    Exceptions inside a SQLite trace callback are swallowed by the sqlite3
    module, so a broken barrier is recorded in ``barrier_errors`` and
    asserted on, never left to vanish.
    """
    conn = state_db.open_rw(hub_db, check_same_thread=False)

    def park_before_grant_update(sql: str) -> None:
        if not sql.lstrip().upper().startswith(_GRANT_UPDATE_PREFIX):
            return
        arrivals.append(machine_id)
        try:
            barrier.wait()
        except threading.BrokenBarrierError as exc:
            barrier_errors.append(exc)

    conn.set_trace_callback(park_before_grant_update)
    try:
        outcomes[machine_id] = enrollment_credentials.resolve_enrollment_identity(
            conn,
            enrollment_credentials.GrantCredential(value=token),
            machine_id=machine_id,
        )
    except enrollment_credentials.EnrollmentCredentialError as exc:
        outcomes[machine_id] = exc
    finally:
        conn.close()


def test_two_machines_racing_one_grant_resolve_exactly_one_owner(
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """if two machines racing on one grant can both resolve an owner then broken"""
    print("if two machines racing on one grant can both resolve an owner then broken")
    caplog.set_level(logging.WARNING, logger=enrollment_credentials.__name__)
    token = mint_grant(enroll_hub_dir)
    hub_db = client.state_db_path(enroll_hub_dir)
    machine_ids = [
        machine_identity.get_or_create_machine_id(path)
        for path in (enroll_spoke_dir, enroll_other_spoke_dir)
    ]
    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_S)
    arrivals: list[str] = []
    barrier_errors: list[BaseException] = []
    outcomes: dict[str, object] = {}
    threads = [
        threading.Thread(
            target=_redeem_behind_barrier,
            args=(hub_db, token, machine_id, barrier, arrivals, barrier_errors, outcomes),
        )
        for machine_id in machine_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=BARRIER_TIMEOUT_S * 2)
        assert not thread.is_alive(), "a redeeming thread hung past the barrier timeout"

    # The race was actually staged: both machines read the grant as unspent
    # and reached the write together. Without this, a pass could mean the
    # threads simply ran one after the other.
    assert sorted(arrivals) == sorted(machine_ids), (
        f"both threads must reach the grant UPDATE, arrivals were {arrivals}"
    )
    assert not barrier_errors, f"the barrier broke, so no race was staged: {barrier_errors}"

    winners = {
        mid: out for mid, out in outcomes.items() if isinstance(out, enrollment.OwnerIdentity)
    }
    losers = {
        mid: out
        for mid, out in outcomes.items()
        if isinstance(out, enrollment_credentials.EnrollmentCredentialError)
    }
    assert len(outcomes) == 2, f"each thread must report an outcome, got {outcomes}"
    assert len(winners) == 1, (
        f"exactly one machine may redeem a single-use grant; {len(winners)} did: {sorted(winners)}"
    )
    assert len(losers) == 1, f"the other machine must be refused, got {outcomes}"
    ((winner_id, owner),) = winners.items()
    ((loser_id, refusal),) = losers.items()
    assert owner.google_sub == ENROLL_OWNER_SUB
    assert "already redeemed by another machine" in str(refusal), str(refusal)
    # The refusal reaches whoever holds the token, so the winner's id stays in
    # the hub log. Presence first: the log names the winner, then the wire does not.
    assert any(
        winner_id in record.getMessage() and loser_id in record.getMessage()
        for record in caplog.records
    ), f"the hub log must name the winning machine; records: {caplog.records}"
    assert winner_id not in str(refusal), "a spent grant must not disclose who spent it"

    row = _grant_row(enroll_hub_dir, token)
    assert row["redeemed_machine_id"] == winner_id, (
        f"the grant records {row['redeemed_machine_id']} as its redeemer, but "
        f"{winner_id} is the machine that was given an owner; the loser "
        f"{loser_id} overwrote the winner's claim"
    )


def _live_attempt(
    hub_url: str,
    data_dir: Path,
    name: str,
    token: str,
    barrier: threading.Barrier,
    results: dict[str, object],
) -> None:
    body = {
        "machine": machine_payload(data_dir, name=name),
        "schema_version": state_schema.SCHEMA_VERSION,
        "credential": {"kind": "grant", "value": token},
    }
    transport = HttpTransport(hub_url)
    barrier.wait()
    try:
        results[name] = transport.post(ENROLL_PATH, body)
    except client.SyncTransportError as exc:
        results[name] = exc


def test_two_live_enrollments_racing_one_grant_admit_exactly_one(
    enroll_live_hub: str, enroll_hub_dir: Path, tmp_path: Path
) -> None:
    """if the loser of a live race on one grant gets anything but a declared 401 then broken"""
    print("if the loser of a live race on one grant gets anything but a declared 401 then broken")
    for round_index in range(LIVE_RACE_ROUNDS):
        token = mint_grant(enroll_hub_dir)
        barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_S)
        results: dict[str, object] = {}
        threads = []
        for side in ("a", "b"):
            data_dir = tmp_path / f"race-{round_index}-{side}"
            data_dir.mkdir()
            threads.append(
                threading.Thread(
                    target=_live_attempt,
                    args=(
                        enroll_live_hub,
                        data_dir,
                        f"race-{round_index}-{side}",
                        token,
                        barrier,
                        results,
                    ),
                )
            )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=BARRIER_TIMEOUT_S * 2)
            assert not thread.is_alive(), f"round {round_index}: a request hung"

        admitted = [out for out in results.values() if isinstance(out, dict)]
        refused = [out for out in results.values() if isinstance(out, client.SyncTransportError)]
        assert len(results) == 2, f"round {round_index}: missing outcomes {results}"
        assert len(admitted) == 1, (
            f"round {round_index}: exactly one enrollment may spend the grant, "
            f"{len(admitted)} did: {results}"
        )
        assert len(refused) == 1, f"round {round_index}: {results}"
        message = str(refused[0])
        assert "HTTP 401" in message and "SYNC_ENROLL_CREDENTIAL" in message, (
            f"round {round_index}: the loser must get the declared 401, got {message}"
        )
        assert _grant_row(enroll_hub_dir, token)["redeemed_machine_id"] == admitted[0]["machine_id"]

    assert len(owner_rows(enroll_hub_dir)) == LIVE_RACE_ROUNDS, (
        "one owned machine per raced grant, no more and no fewer"
    )


def test_the_same_machine_re_presenting_its_own_grant_still_resolves(
    enroll_hub_dir: Path, enroll_spoke_dir: Path
) -> None:
    """if the race guard refuses a machine re-presenting its own spent grant then broken"""
    print("if the race guard refuses a machine re-presenting its own spent grant then broken")
    token = mint_grant(enroll_hub_dir)
    machine_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    conn: sqlite3.Connection = state_db.open_rw(client.state_db_path(enroll_hub_dir))
    try:
        credential = enrollment_credentials.GrantCredential(value=token)
        first = enrollment_credentials.resolve_enrollment_identity(
            conn, credential, machine_id=machine_id
        )
        again = enrollment_credentials.resolve_enrollment_identity(
            conn, credential, machine_id=machine_id
        )
    finally:
        conn.close()
    assert first == again, "re-running enroll on the redeeming machine must stay idempotent"
    assert _grant_row(enroll_hub_dir, token)["redeemed_machine_id"] == machine_id
