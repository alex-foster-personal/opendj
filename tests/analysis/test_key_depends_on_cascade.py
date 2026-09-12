"""Queue cascade reads the real key payload ``depends_on.beatgrid`` block.

nav1-key-record writes the five-field identity on
``record.lanes["key"].payload["depends_on"]["beatgrid"]``; the queue must read
and hash that block (not only ``features_blob``) so a current key is not
re-queued and a stale key is.

-Claude
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis import queue_store
from apps.analysis.backends.own_key import BACKEND_NAME, PRODUCER_VERSION
from apps.analysis.canonical import canonical_pointer
from apps.analysis.depends_on import (
    beatgrid_record_digest as queue_beatgrid_digest,
    declared_dependency,
    dependency_identity,
)
from apps.analysis.queue import cascade_dependents
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_key import lane_payload

from .test_key_depends_on_staleness import (
    FINGERPRINT_V1,
    FINGERPRINT_V2,
    _beatgrid_record,
    _depends_on_for,
    _key_record,
    state_db,
)

pytestmark = pytest.mark.requirement("NATIVE-10")


def test_the_two_digest_helpers_agree_on_the_same_payload() -> None:
    grid = _beatgrid_record(
        "t-digest-agree",
        backend_version="1.0.0",
        decode_fingerprint=FINGERPRINT_V1,
    )
    payload_digest = lane_payload.beatgrid_record_digest(grid.lanes["beatgrid"].payload)
    record_digest = queue_beatgrid_digest(grid)
    assert record_digest == payload_digest


def test_declared_dependency_reads_the_key_payload_block(state_db: Path) -> None:
    stable_id = "t-declared-payload"
    grid = _beatgrid_record(
        stable_id,
        backend_version="1.0.0",
        decode_fingerprint=FINGERPRINT_V1,
    )
    key = _key_record(stable_id, depends_on_beatgrid=_depends_on_for(grid))
    assert key.features_blob.get("depends_on") is None
    block = key.lanes["key"].payload["depends_on"]["beatgrid"]
    assert declared_dependency(key, "beatgrid") == block
    assert declared_dependency(key, "beatgrid") == dependency_identity(grid)


def test_cascade_does_not_requeue_a_matching_real_key_record(state_db: Path) -> None:
    stable_id = "t-cascade-match"
    grid = _beatgrid_record(
        stable_id,
        backend_version="1.0.0",
        decode_fingerprint=FINGERPRINT_V1,
    )
    upsert_record(grid, db_path=state_db)
    key = _key_record(stable_id, depends_on_beatgrid=_depends_on_for(grid))
    upsert_record(key, db_path=state_db)

    conn = open_conn(state_db)
    try:
        outcomes = cascade_dependents(
            conn,
            stable_id=stable_id,
            lane="beatgrid",
            dependency_record=grid,
        )
        assert [o for o in outcomes if o.requeued] == []
        assert queue_store.stale_rows(conn, stable_id, "key") == set()
    finally:
        conn.close()


def test_cascade_requeues_when_beatgrid_version_moves(state_db: Path) -> None:
    stable_id = "t-cascade-version"
    grid_v1 = _beatgrid_record(
        stable_id,
        backend_version="1.0.0",
        decode_fingerprint=FINGERPRINT_V1,
    )
    upsert_record(grid_v1, db_path=state_db)
    key = _key_record(stable_id, depends_on_beatgrid=_depends_on_for(grid_v1))
    upsert_record(key, db_path=state_db)

    grid_v2 = _beatgrid_record(
        stable_id,
        backend_version="1.1.0",
        decode_fingerprint=FINGERPRINT_V1,
    )

    conn = open_conn(state_db)
    try:
        assert canonical_pointer(conn, stable_id, "key") == (
            BACKEND_NAME,
            PRODUCER_VERSION,
        )
        outcomes = cascade_dependents(
            conn,
            stable_id=stable_id,
            lane="beatgrid",
            dependency_record=grid_v2,
        )
        requeued = [o for o in outcomes if o.lane == "key" and o.requeued]
        assert requeued, outcomes
        assert queue_store.stale_rows(conn, stable_id, "key") == {
            (BACKEND_NAME, PRODUCER_VERSION)
        }
    finally:
        conn.close()

    upsert_record(grid_v2, db_path=state_db)
    conn = open_conn(state_db)
    try:
        assert canonical_pointer(conn, stable_id, "key") is None
    finally:
        conn.close()


def test_cascade_requeues_when_only_decode_fingerprint_moves(state_db: Path) -> None:
    stable_id = "t-cascade-fingerprint"
    grid_v1 = _beatgrid_record(
        stable_id,
        backend_version="1.1.0",
        decode_fingerprint=FINGERPRINT_V1,
    )
    upsert_record(grid_v1, db_path=state_db)
    key = _key_record(stable_id, depends_on_beatgrid=_depends_on_for(grid_v1))
    upsert_record(key, db_path=state_db)

    grid_v2 = _beatgrid_record(
        stable_id,
        backend_version="1.1.0",
        decode_fingerprint=FINGERPRINT_V2,
    )

    conn = open_conn(state_db)
    try:
        assert canonical_pointer(conn, stable_id, "key") == (
            BACKEND_NAME,
            PRODUCER_VERSION,
        )
        outcomes = cascade_dependents(
            conn,
            stable_id=stable_id,
            lane="beatgrid",
            dependency_record=grid_v2,
        )
        requeued = [o for o in outcomes if o.lane == "key" and o.requeued]
        assert requeued, outcomes
        assert queue_store.stale_rows(conn, stable_id, "key") == {
            (BACKEND_NAME, PRODUCER_VERSION)
        }
    finally:
        conn.close()

    upsert_record(grid_v2, db_path=state_db)
    conn = open_conn(state_db)
    try:
        assert canonical_pointer(conn, stable_id, "key") is None
    finally:
        conn.close()
