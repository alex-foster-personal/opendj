"""[if] hash_pending spoke/hub sync runs [then] deferred hashes follow ADR-0068, [else stop].

ADR-0068 / CLOUDSYNC-19 acceptance: hash_pending deferred hash sync.

Acceptance, one test each (fixtures use 100/80 rows in CI; REQUIREMENTS.md cites 9000/8000):
- inferred/no-hash/no-ISRC spoke -> verified agreement, hash_pending=N, quarantined=0
- second machine --for-hub hashes most pending rows -> hub reports remainder
- two machines push different hashes -> LWW, both logged, one row
- old hub without hash-pending/v1 -> 422 and versioned status upgrade message
- row with content_hash is never hash_pending
- hash_pending parent takes its children into the offer
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.shared.state.backfill_content_hash import run_for_hub_backfill
from apps.sync_hub import capabilities, client, engine, maintenance, protocol, sync_set
from apps.sync_hub.hash_pending import count_hash_pending
from apps.sync_hub.transport import refused
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_hub_sync import _DEV_A, _T0, _T1, _T2
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _HASH_B,
    _hub_app,
    _insert_identified_track,
    _values,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-19")

_PENDING_COUNT = 100


def _hub_transport(hub_dir: Path) -> TestClientTransport:
    return TestClientTransport(TestClient(_hub_app(hub_dir)))


def _seed_pending_tracks(
    conn: sqlite3.Connection, count: int, *, origin: str = _DEV_A
) -> None:
    rows = [
        (
            f"trk-pending-{index:05d}",
            "inferred",
            f"title-{index}",
            None,
            None,
            None,
            _T0,
            _T0,
            origin,
            None,
        )
        for index in range(count)
    ]
    conn.executemany(
        """
        INSERT INTO tracks(
            stable_id, stable_id_tier, title, isrc, file_path, content_hash,
            created_at, updated_at, origin_device_id, deleted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


class _OldHashPendingHubTransport:
    """Hub double that lacks hash-pending/v1 on hello and refuses pending pushes."""

    __test__ = False

    def __init__(self, http: TestClient) -> None:
        self._http = http

    def _decoded(self, response: Any, label: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise refused(label, response.status_code, response.text)
        return dict(response.json())

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        if path.endswith("/push"):
            pending = sum(
                1 for row in body.get("rows", []) if row.get("hash_pending")
            )
            if pending:
                message = capabilities.hash_pending_refusal(
                    "push", pending, body.get("capabilities")
                )
                raise refused(
                    f"POST {path}",
                    422,
                    json.dumps({"detail": {"code": "SYNC_PROTOCOL", "message": message}}),
                )
        response = self._http.post(path, json=body)
        decoded = self._decoded(response, f"POST {path}")
        if path.endswith("/hello") and "capabilities" in decoded:
            decoded["capabilities"] = [
                token
                for token in decoded["capabilities"]
                if token != capabilities.HASH_PENDING_V1
            ]
        return decoded

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        response = self._http.get(path, params=dict(params))
        return self._decoded(response, f"GET {path}")


def test_spoke_offers_hash_pending_inferred_without_audio(tmp_path: Path) -> None:
    """9000 inferred/no hash/no ISRC -> sync ok, hash_pending=9000, quarantined=0."""
    spoke = tmp_path / "spoke"
    hub_dir = tmp_path / "hub"
    conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _seed_pending_tracks(conn, _PENDING_COUNT)
        conn.commit()
    finally:
        conn.close()
    state_db.open_rw(client.state_db_path(hub_dir)).close()

    transport = _hub_transport(hub_dir)
    result = client.run_sync(
        spoke, "http://hub.invalid", transport=transport, name="spoke"
    )

    assert result.digest_inconclusive is False
    assert result.hash_pending == _PENDING_COUNT
    assert result.quarantined_rows == 0
    assert result.quarantined == 0
    hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        assert count_hash_pending(hub_conn) == _PENDING_COUNT
    finally:
        hub_conn.close()


def test_hash_pending_track_and_children_in_offer(tmp_path: Path) -> None:
    """Children of a hash_pending parent travel in the offer."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _insert_identified_track(
            conn, "trk-parent", title="pending", updated_at=_T0, origin=_DEV_A
        )
        conn.execute(
            "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id, "
            "updated_at, origin_device_id) "
            "VALUES ('trk-parent', 'rekordbox', '1', ?, ?)",
            (_T0, _DEV_A),
        )
        conn.commit()
        offer = engine.spoke_push(conn)
        offered = {(change.table, change.pk) for change in offer.rows}
    finally:
        conn.close()

    assert ("tracks", ("trk-parent",)) in offered
    assert ("track_vendor_ids", ("trk-parent", "rekordbox")) in offered
    assert offer.hash_pending == 1
    assert offer.quarantined == 0


def test_for_hub_backfill_resolves_hub_pending(tmp_path: Path, good_wav: Path) -> None:
    """Machine with audio hashes most pending rows; next sync reports the remainder."""
    hub_dir = tmp_path / "hub"
    spoke = tmp_path / "spoke"
    state_db.open_rw(client.state_db_path(hub_dir)).close()

    transport = _hub_transport(hub_dir)
    conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _seed_pending_tracks(conn, _PENDING_COUNT)
        conn.commit()
    finally:
        conn.close()
    client.run_sync(spoke, "http://hub.invalid", transport=transport, name="spoke")

    resolvable = 80
    conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        for index in range(resolvable):
            conn.execute(
                "UPDATE tracks SET file_path = ? WHERE stable_id = ?",
                (str(good_wav), f"trk-pending-{index:05d}"),
            )
        conn.commit()
    finally:
        conn.close()

    report = run_for_hub_backfill(
        spoke, live=True, transport=transport, hub_endpoint="http://hub.invalid"
    )
    assert report.total == _PENDING_COUNT
    assert report.hashed == resolvable

    hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        assert count_hash_pending(hub_conn) == _PENDING_COUNT - resolvable
    finally:
        hub_conn.close()

    result = client.run_sync(spoke, "http://hub.invalid", transport=transport, name="spoke")
    assert result.digest_inconclusive is False
    assert result.hash_pending == _PENDING_COUNT - resolvable


@pytest.fixture
def good_wav(tmp_path: Path) -> Path:
    import wave

    wav_path = tmp_path / "good.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(44100)
        fh.writeframes(b"\x00\x00" * 4410)
    return wav_path


def test_hub_hash_conflict_last_writer_wins(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two hashes for one row: later stamp wins, both values logged, one row."""
    hub_dir = tmp_path / "hub"
    hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        _insert_identified_track(
            hub_conn,
            "trk-conflict",
            title="pending",
            updated_at=_T0,
            origin=_DEV_A,
        )
        hub_conn.commit()
    finally:
        hub_conn.close()

    transport = _hub_transport(hub_dir)
    machine_a = sync_stamp.local_machine_id(
        state_db.open_rw(client.state_db_path(tmp_path / "a"))
    )
    state_db.open_rw(client.state_db_path(tmp_path / "a")).close()
    machine_b = sync_stamp.local_machine_id(
        state_db.open_rw(client.state_db_path(tmp_path / "b"))
    )
    state_db.open_rw(client.state_db_path(tmp_path / "b")).close()

    def _push_hash(machine_id: str, digest: str, updated_at: str) -> None:
        hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
        try:
            values = _values(
                hub_conn,
                "tracks",
                stable_id="trk-conflict",
                stable_id_tier="inferred",
                title="pending",
                content_hash=digest,
                updated_at=updated_at,
                origin_device_id=machine_id,
                created_at=_T0,
                deleted_at=None,
            )
            engine.hub_apply(
                hub_conn,
                [
                    protocol.RowChange(
                        table="tracks",
                        pk=("trk-conflict",),
                        values=values,
                        hash_pending=False,
                    )
                ],
            )
            hub_conn.commit()
        finally:
            hub_conn.close()

    with caplog.at_level(logging.WARNING):
        _push_hash(machine_a, _HASH_A, _T1)
        _push_hash(machine_b, _HASH_B, _T2)

    hub_after = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        row = hub_after.execute(
            "SELECT content_hash FROM tracks WHERE stable_id = 'trk-conflict'"
        ).fetchone()
        count = hub_after.execute(
            "SELECT COUNT(*) FROM tracks WHERE stable_id = 'trk-conflict'"
        ).fetchone()[0]
    finally:
        hub_after.close()
    assert count == 1
    assert row[0] == _HASH_B
    assert "conflicting content_hash" in caplog.text
    assert _HASH_A in caplog.text
    assert _HASH_B in caplog.text


def test_old_hub_refuses_hash_pending_with_422(tmp_path: Path) -> None:
    """Old hub -> 422; status journal carries hash-pending upgrade message."""
    spoke = tmp_path / "spoke"
    hub_dir = tmp_path / "hub"
    conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _insert_identified_track(
            conn, "trk-pending", title="no identity", updated_at=_T0, origin=_DEV_A
        )
        conn.commit()
    finally:
        conn.close()
    state_db.open_rw(client.state_db_path(hub_dir)).close()

    with TestClient(_hub_app(hub_dir)) as http:
        transport = _OldHashPendingHubTransport(http)
        with pytest.raises(client.SyncTransportError):
            maintenance.sync(
                spoke, "http://hub.invalid", transport=transport, name="spoke"
            )

    from apps.sync_hub import status as sync_status

    current = sync_status.read_status(spoke)
    last = current.last_result
    assert last is not None
    assert last["status"] == "error"
    assert capabilities.HASH_PENDING_V1 in last["message"]


def test_content_hash_row_never_hash_pending(tmp_path: Path) -> None:
    """Negative control: hashed rows are not hash_pending candidates."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _insert_identified_track(
            conn,
            "trk-hashed",
            title="hashed",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.commit()
        assert sync_set.is_hash_pending_track(
            {
                "stable_id": "trk-hashed",
                "stable_id_tier": "inferred",
                "content_hash": _HASH_A,
                "isrc": None,
            },
            sync_set.HeldKeys(conn),
        ) is False
        with pytest.raises(protocol.SyncProtocolError):
            protocol.validate_hash_pending_row(
                "tracks",
                {
                    "stable_id": "trk-hashed",
                    "stable_id_tier": "inferred",
                    "content_hash": _HASH_A,
                    "isrc": None,
                },
                True,
            )
    finally:
        conn.close()

