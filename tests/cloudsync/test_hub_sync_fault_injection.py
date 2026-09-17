"""Network faults mid-sync, injected through :class:`FaultTransport` (plan W9).

[if] a lost response loses rows, duplicates them, or skips the fence [then] fail, [else stop].

Idempotent replay (``apps/sync_hub/client.py``) was proven only by the
uncommitted round 1-2 probes; nothing in the suite ever lost a response. This
module commits those proofs as regressions. The hub is the real router over
the real ASGI stack and every spoke is a real migrated state DB. Only the
wire is broken, and only where a test says so.

The central case is the most common real network failure: the hub COMMITS a
push chunk and the answer never comes back. The two sides then disagree
about what happened, and the protocol has to reconcile them without losing a
row, without the hub logging the same row twice, and without the spoke
stepping its push fence over rows nobody acknowledged.

Acceptance, one test each ("strands" covers the three quieter failures: a
duplicate hub_changelog entry, a push fence that steps over an unacknowledged
chunk, and a retry that does not converge):
- if a push answer lost after commit loses, double-logs or strands a row then broken
- if a push chunk that never left the spoke is not delivered by the next sync then broken
- if a pull response lost mid-drain loses a row or blocks convergence then broken
- if a digest lost after an acknowledged round moves the push fence then broken
- if an armed fault that never fires lets a test pass then broken
- if a fault whose call the hub itself refused counts as fired then broken
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync_hub import client, engine, service, wire_version

from .enrollment_transport import TestClientTransport
from .fault_transport import FaultInjected, FaultTransport
from .test_hub_sync import _DEV_A, _T1, _T2, _open, _set_track_title

pytestmark = pytest.mark.requirement("CLOUDSYNC-04")

#: Small enough that five rows cross in three push chunks, so a fault can
#: land on a chunk that is neither the first nor the last.
PUSH_CHUNK_ROWS: int = 2
PULL_CHUNK_ROWS: int = 2
ROW_COUNT: int = 5
TRACK_IDS: tuple[str, ...] = tuple(f"trk-{index}" for index in range(ROW_COUNT))


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunk sizes the client reads at call time, lowered so faults can land mid-stream."""
    monkeypatch.setattr(client, "PUSH_BATCH_ROWS", PUSH_CHUNK_ROWS)
    monkeypatch.setattr(client, "PULL_LIMIT", PULL_CHUNK_ROWS)


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


def _sync(data_dir: Path, transport: client.HubTransport, name: str) -> client.SyncResult:
    return client.run_sync(data_dir, "http://hub.invalid", transport=transport, name=name)


def _insert_tracks(data_dir: Path, stable_ids: tuple[str, ...], *, updated_at: str) -> None:
    conn = _open(data_dir)
    try:
        for stable_id in stable_ids:
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
                "created_at, updated_at, origin_device_id) "
                "VALUES (?, 'inferred', 'seed', ?, ?, ?, ?)",
                (
                    stable_id,
                    hashlib.sha256(stable_id.encode("utf-8")).hexdigest(),
                    updated_at,
                    updated_at,
                    _DEV_A,
                ),
            )
    finally:
        conn.close()


def _edit_titles(data_dir: Path, stable_ids: tuple[str, ...], *, updated_at: str) -> None:
    conn = _open(data_dir)
    try:
        for stable_id in stable_ids:
            _set_track_title(
                conn, stable_id, title=f"edited {stable_id}", updated_at=updated_at, origin=_DEV_A
            )
    finally:
        conn.close()


def _titles(data_dir: Path) -> dict[str, str]:
    conn = _open(data_dir)
    try:
        return {
            str(row[0]): str(row[1]) for row in conn.execute("SELECT stable_id, title FROM tracks")
        }
    finally:
        conn.close()


def _changelog_entries(hub_dir: Path) -> list[str]:
    """Every ``hub_changelog`` row_pk for ``tracks``, one per entry, duplicates kept."""
    conn = _open(hub_dir)
    try:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT row_pk FROM hub_changelog WHERE table_name = 'tracks' ORDER BY seq"
            )
        ]
    finally:
        conn.close()


def _push_fence(data_dir: Path, hub_machine_id: str) -> int:
    conn: sqlite3.Connection = _open(data_dir)
    try:
        return engine.read_watermark(conn, hub_machine_id).last_push_seq
    finally:
        conn.close()


def _local_seq(data_dir: Path) -> int:
    conn = _open(data_dir)
    try:
        return engine.local_seq(conn)
    finally:
        conn.close()


def _bootstrap(spoke: Path, hub: TestClientTransport) -> client.SyncResult:
    """Seed the rows and complete one clean sync, so later syncs are FENCED offers."""
    _insert_tracks(spoke, TRACK_IDS, updated_at=_T1)
    first = _sync(spoke, hub, "spoke-a")
    assert first.accepted == ROW_COUNT, f"bootstrap pushed {first.accepted} of {ROW_COUNT}"
    return first


# ----- push: the answer is lost after the hub commits ----------------------


def test_a_push_response_dropped_after_commit_loses_nothing_and_converges(
    hub: TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """if a push answer lost after commit loses, double-logs or strands a row then broken"""
    print("if a push answer lost after commit loses, double-logs or strands a row then broken")
    first = _bootstrap(spoke_a, hub)
    fence_before = _push_fence(spoke_a, first.hub_machine_id)
    entries_before = _changelog_entries(hub_dir)
    _edit_titles(spoke_a, TRACK_IDS, updated_at=_T2)
    edited = _titles(spoke_a)

    faulty = FaultTransport(hub)
    faulty.fail_on_nth("push", 2, mode="drop_response_after_commit")
    with pytest.raises(FaultInjected, match="hub committed it"):
        _sync(spoke_a, faulty, "spoke-a")
    faulty.assert_all_fired()

    # The fault landed where it was aimed: chunk 1 answered, chunk 2
    # committed with its answer lost, chunk 3 was never offered.
    assert [call.outcome for call in faulty.calls_to("push")] == [
        "delivered",
        "dropped_after_commit",
    ]
    hub_titles = _titles(hub_dir)
    committed = [sid for sid in TRACK_IDS if hub_titles[sid] == edited[sid]]
    assert committed == list(TRACK_IDS[: PUSH_CHUNK_ROWS * 2]), (
        f"the hub should hold exactly the two committed chunks, holds edits for {committed}"
    )
    assert _titles(spoke_a) == edited, "the spoke lost a local edit to a dropped response"
    assert _push_fence(spoke_a, first.hub_machine_id) == fence_before, (
        "the push fence moved although the spoke never saw an acknowledgement "
        "for the round; the unacknowledged rows would never be offered again"
    )

    recovered = _sync(spoke_a, hub, "spoke-a")
    assert recovered.pushed == ROW_COUNT, "the retry must re-offer every row above the fence"
    assert recovered.accepted == ROW_COUNT - PUSH_CHUNK_ROWS * 2, (
        f"only the never-sent chunk is new to the hub; it accepted {recovered.accepted}"
    )
    assert recovered.rejected == PUSH_CHUNK_ROWS * 2, (
        "the replayed committed rows must lose the equal-stamp compare, not re-apply"
    )
    assert _titles(hub_dir) == edited, "the hub did not converge on every edit"

    entries = _changelog_entries(hub_dir)
    new_entries = entries[len(entries_before) :]
    assert sorted(new_entries) == sorted(set(new_entries)), (
        f"a replayed row was logged twice in hub_changelog: {new_entries}"
    )
    assert len(new_entries) == ROW_COUNT, (
        f"each edited row needs exactly one new hub_changelog entry, got {len(new_entries)}"
    )
    assert _push_fence(spoke_a, first.hub_machine_id) == _local_seq(spoke_a), (
        "after a clean retry the fence must cover every local write"
    )


# ----- push: the request never leaves the spoke ----------------------------


def test_a_push_chunk_that_never_left_the_spoke_lands_on_the_next_sync(
    hub: TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """if a push chunk that never left the spoke is not delivered by the next sync then broken"""
    print("if a push chunk that never left the spoke is not delivered by the next sync then broken")
    first = _bootstrap(spoke_a, hub)
    fence_before = _push_fence(spoke_a, first.hub_machine_id)
    _edit_titles(spoke_a, TRACK_IDS, updated_at=_T2)
    edited = _titles(spoke_a)

    faulty = FaultTransport(hub)
    faulty.fail_on_nth("push", 2, mode="fail_before_send")
    with pytest.raises(FaultInjected, match="hub never saw it"):
        _sync(spoke_a, faulty, "spoke-a")
    faulty.assert_all_fired()

    hub_titles = _titles(hub_dir)
    assert [sid for sid in TRACK_IDS if hub_titles[sid] == edited[sid]] == list(
        TRACK_IDS[:PUSH_CHUNK_ROWS]
    ), "only the first chunk may have reached the hub"
    assert _push_fence(spoke_a, first.hub_machine_id) == fence_before

    recovered = _sync(spoke_a, hub, "spoke-a")
    assert recovered.accepted == ROW_COUNT - PUSH_CHUNK_ROWS
    assert _titles(hub_dir) == edited


# ----- pull: the answer is lost mid-drain ----------------------------------


def test_a_pull_response_lost_mid_drain_re_pulls_and_converges(
    hub: TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """if a pull response lost mid-drain loses a row or blocks convergence then broken"""
    print("if a pull response lost mid-drain loses a row or blocks convergence then broken")
    _bootstrap(spoke_a, hub)

    faulty = FaultTransport(hub)
    faulty.fail_on_nth("pull", 2, mode="drop_response_after_commit")
    with pytest.raises(FaultInjected):
        _sync(spoke_b, faulty, "spoke-b")
    faulty.assert_all_fired()
    partial = _titles(spoke_b)
    assert len(partial) == 0, (
        "_pull_in_chunks buffers every RowChange across the whole drain and "
        "applies once in a single spoke_apply after has_more is false "
        "(apps.sync_hub.client_transport_ops docstring: per-chunk apply dies "
        "on FK, since a child row in an early window can name a parent "
        "logged in a later one). Losing the 2nd chunk's response raises "
        "before that final apply ever runs, so nothing this round landed on "
        f"B, not just the lost chunk; found {len(partial)}"
    )

    recovered = _sync(spoke_b, hub, "spoke-b")
    assert recovered.applied == ROW_COUNT, (
        "no watermark was written on the faulted round (the exception "
        "propagates out of _pull_in_chunks before its caller settles "
        "last_pull_seq), so the recovery sync re-pulls from the original "
        "floor and must apply every row, not just the ones the lost chunk "
        "would have carried"
    )
    assert _titles(spoke_b) == _titles(hub_dir) == _titles(spoke_a)


# ----- digest: the round completed, only the comparison was lost ------------


def test_a_digest_lost_after_an_acknowledged_round_keeps_the_fence_honest(
    hub: TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """if a digest lost after an acknowledged round moves the push fence then broken"""
    print("if a digest lost after an acknowledged round moves the push fence then broken")
    first = _bootstrap(spoke_a, hub)
    _edit_titles(spoke_a, TRACK_IDS, updated_at=_T2)

    faulty = FaultTransport(hub)
    faulty.fail_on_nth("digest", 1, mode="fail_before_send")
    with pytest.raises(FaultInjected):
        _sync(spoke_a, faulty, "spoke-a")
    faulty.assert_all_fired()
    assert [call.outcome for call in faulty.calls_to("push")] == ["delivered"] * 3
    # Every chunk was acknowledged before the digest was lost, so the fence
    # records them: it must sit at the local changelog head, no lower (the
    # retry would re-offer acknowledged rows) and no higher (impossible).
    assert _push_fence(spoke_a, first.hub_machine_id) == _local_seq(spoke_a)

    retry = _sync(spoke_a, hub, "spoke-a")
    assert retry.pushed == 0, "a retry after an acknowledged round has nothing to offer"
    assert _titles(hub_dir) == _titles(spoke_a)


# ----- the harness itself --------------------------------------------------


def test_a_fault_that_never_fires_fails_the_test(hub: TestClientTransport, spoke_a: Path) -> None:
    """if an armed fault that never fires lets a test pass then broken"""
    print("if an armed fault that never fires lets a test pass then broken")
    _insert_tracks(spoke_a, ("trk-only",), updated_at=_T1)
    faulty = FaultTransport(hub)
    faulty.fail_on_nth("push", 2, mode="drop_response_after_commit")
    result = _sync(spoke_a, faulty, "spoke-a")
    assert result.push_requests == 1, "one row is one push chunk, so call #2 never happens"
    with pytest.raises(AssertionError, match="never fired"):
        faulty.assert_all_fired()
    # Positive control: the same transport records every call it forwarded.
    assert [call.outcome for call in faulty.calls_to("push")] == ["delivered"]
    assert faulty.calls_to("hello")[0].reached_hub


def test_a_fault_preempted_by_a_real_hub_error_stays_pending(
    hub: TestClientTransport, spoke_a: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if a fault whose call the hub itself refused counts as fired then broken"""
    print("if a fault whose call the hub itself refused counts as fired then broken")
    _insert_tracks(spoke_a, ("trk-only",), updated_at=_T1)
    # The spoke's build claims another WIRE version. Schema is local storage
    # (wire_version.incompatibility): a matching wire lets rows cross even
    # when SCHEMA_VERSION differs, so patching schema would not 409. The
    # hub must refuse hello itself before the armed drop can count as fired.
    monkeypatch.setattr(
        client,
        "wire_version",
        SimpleNamespace(
            WIRE_VERSION=wire_version.WIRE_VERSION + 1,
            incompatibility=wire_version.incompatibility,
        ),
    )
    faulty = FaultTransport(hub)
    faulty.drop_response_after_commit(1, path="hello")
    with pytest.raises(client.SyncTransportError, match="SYNC_WIRE_VERSION") as excinfo:
        _sync(spoke_a, faulty, "spoke-a")
    assert not isinstance(excinfo.value, FaultInjected), "the hub's own error must surface"
    assert [call.outcome for call in faulty.calls_to("hello")] == ["hub_error"]
    with pytest.raises(AssertionError, match="preempted"):
        faulty.assert_all_fired()
