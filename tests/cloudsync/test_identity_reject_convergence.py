"""CLOUDSYNC-07: hub-authority identity-collapse rejection convergence (issue #3057).

When hub and spoke elect opposite survivors for the same content identity,
the push response must name the hub survivor so the spoke can converge.

[if] mirror-image elections diverge [then] one sync converges without digest mismatch, [else stop].
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.sync_hub import capabilities, client, engine, protocol, sync_set, wire_version
from apps.sync_hub.engine_identity import _follow_remap
from apps.sync_hub.engine_identity_map import (
    apply_hub_identity_rejects,
    effective_identity_remap,
    load_identity_remap,
    record_identity_remap,
)
from tests.cloudsync.test_hub_sync import _DEV_A, _DEV_B, _T0, _TestClientTransport
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _hub_app,
    _insert_field,
    _insert_identified_track,
    _insert_location,
    _incoming_track,
    _open_hub,
    _track_ids,
    _values,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")

_PK_HUB = "a00000000000000000000000000000000000000001"
_PK_SPOKE = "b00000000000000000000000000000000000000002"
_PK_A = "c00000000000000000000000000000000000000003"
_PK_B = "d00000000000000000000000000000000000000004"
_PK_C = "e00000000000000000000000000000000000000005"
_STAMP = "2026-09-12T16:45:34.823926+00:00"


def _spoke_row_values(
    conn: sqlite3.Connection, stable_id: str, **overrides: object
) -> dict[str, Any]:
    return _values(
        conn,
        "tracks",
        stable_id=stable_id,
        stable_id_tier="inferred",
        title="mirror track",
        content_hash=_HASH_A,
        created_at=_T0,
        updated_at=_STAMP,
        origin_device_id=_DEV_A,
        deleted_at=None,
        **overrides,
    )


def test_push_response_includes_identity_rejects(tmp_path: Path) -> None:
    """Hub apply and HTTP push both name the survivor on identity-collapse reject."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            _PK_HUB,
            title="hub survivor",
            content_hash=_HASH_A,
            updated_at=_STAMP,
            origin=_DEV_A,
        )
        conn.commit()
        incoming = _incoming_track(
            conn,
            _PK_SPOKE,
            title="spoke offered",
            updated_at=_STAMP,
            content_hash=_HASH_A,
        )
        incoming = protocol.RowChange(
            table=incoming.table,
            pk=incoming.pk,
            values={**incoming.values, "origin_device_id": _DEV_A},
        )
        result = engine.hub_apply(conn, [incoming])
        assert result.rejected == 1
        assert result.identity_rejects == (
            protocol.IdentityReject(
                table="tracks",
                offered_pk=_PK_SPOKE,
                survivor_pk=_PK_HUB,
            ),
        )
    finally:
        conn.close()

    hub_dir = tmp_path / "hub-http"
    hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        _insert_identified_track(
            hub_conn,
            _PK_HUB,
            title="hub survivor",
            content_hash=_HASH_A,
            updated_at=_STAMP,
            origin=_DEV_A,
        )
        spoke_values = _spoke_row_values(hub_conn, _PK_SPOKE)
        hub_conn.commit()
    finally:
        hub_conn.close()

    with TestClient(_hub_app(hub_dir)) as http:
        machine_id = _DEV_B
        http.post(
            "/api/v1/sync/hello",
            json={
                "machine": {
                    "machine_id": machine_id,
                    "name": "spoke",
                    "platform": "macos",
                    "is_hub": False,
                    "data_root": None,
                    "first_seen": _T0,
                    "last_seen": _T0,
                },
                "schema_version": state_schema.SCHEMA_VERSION,
                "wire_version": wire_version.WIRE_VERSION,
                "capabilities": list(capabilities.THIS_BUILD),
                "machines": [],
            },
        ).raise_for_status()
        response = http.post(
            "/api/v1/sync/push",
            json={
                "machine_id": machine_id,
                "schema_version": state_schema.SCHEMA_VERSION,
                "wire_version": wire_version.WIRE_VERSION,
                "capabilities": list(capabilities.THIS_BUILD),
                "rows": [
                    {
                        "table": "tracks",
                        "pk": [_PK_SPOKE],
                        "values": spoke_values,
                    }
                ],
                "machines": [],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["identity_rejects"] == [
        {
            "table": "tracks",
            "offered_pk": _PK_SPOKE,
            "survivor_pk": _PK_HUB,
        }
    ]


def _seed_hub_track(conn: sqlite3.Connection, stable_id: str) -> None:
    """Put one track on the hub through ``hub_apply`` so pull can serve it."""
    incoming = _incoming_track(
        conn,
        stable_id,
        title="hub copy",
        updated_at=_STAMP,
        content_hash=_HASH_A,
    )
    incoming = protocol.RowChange(
        table=incoming.table,
        pk=incoming.pk,
        values={**incoming.values, "origin_device_id": _DEV_A},
    )
    result = engine.hub_apply(conn, [incoming])
    assert result.accepted == 1, result


def test_mirror_election_converges_on_hub_survivor(tmp_path: Path) -> None:
    """Opposite survivors for one identity converge when the hub names its PK."""
    spoke = tmp_path / "spoke"
    hub_dir = tmp_path / "hub"

    hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        _seed_hub_track(hub_conn, _PK_HUB)
        hub_conn.commit()
    finally:
        hub_conn.close()

    spoke_conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _insert_identified_track(
            spoke_conn,
            _PK_SPOKE,
            title="spoke copy",
            content_hash=_HASH_A,
            updated_at=_STAMP,
            origin=_DEV_A,
        )
        _insert_field(
            spoke_conn,
            _PK_SPOKE,
            field_name="bpm",
            value_json="128.0",
            updated_at=_STAMP,
            origin=_DEV_A,
        )
        _insert_location(
            spoke_conn,
            location_id="loc-spoke",
            stable_id=_PK_SPOKE,
            file_path="/Silver/mirror.mp3",
            updated_at=_STAMP,
            origin=_DEV_A,
        )
        spoke_conn.commit()
    finally:
        spoke_conn.close()

    with TestClient(_hub_app(hub_dir)) as http:
        result = client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=_TestClientTransport(http),
            name="spoke",
        )

    spoke_after = state_db.open_rw(client.state_db_path(spoke))
    try:
        assert load_identity_remap(spoke_after)[_PK_SPOKE] == _PK_HUB
        field_owner = spoke_after.execute(
            "SELECT stable_id FROM track_fields WHERE field_name = 'bpm'"
        ).fetchone()
        assert field_owner is not None and str(field_owner[0]) == _PK_HUB
        location_owner = spoke_after.execute(
            "SELECT stable_id FROM track_locations WHERE location_id = 'loc-spoke'"
        ).fetchone()
        assert location_owner is not None and str(location_owner[0]) == _PK_HUB
        assert _PK_HUB in _track_ids(spoke_after)
        if _PK_SPOKE in _track_ids(spoke_after):
            held = sync_set.HeldKeys(spoke_after)
            columns = sync_set.deciding_columns(spoke_after, "tracks")
            spec = sync_set.spec_for("tracks")
            row = spoke_after.execute(
                f"SELECT {', '.join(columns)} FROM tracks WHERE stable_id = ?",
                (_PK_SPOKE,),
            ).fetchone()
            assert (
                sync_set.row_reason("tracks", columns, row, spec, held)
                == sync_set.IDENTITY_DUP_REASON
            )
        local_digest = protocol.sync_digest(spoke_after)
    finally:
        spoke_after.close()

    hub_after = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        remote_digest = protocol.sync_digest(hub_after)
    finally:
        hub_after.close()

    assert local_digest.overall == remote_digest.overall
    assert not result.digest_inconclusive


def test_remap_chain_follows_hub_survivor(tmp_path: Path) -> None:
    """A->B local remap plus hub B->C reject lands offers on C."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        for pk in (_PK_B, _PK_C):
            _insert_identified_track(
                conn,
                pk,
                title=f"track {pk[-1]}",
                content_hash=_HASH_A,
                updated_at=_STAMP,
                origin=_DEV_A,
            )
        record_identity_remap(conn, {}, _PK_A, _PK_B)
        conn.commit()

        apply_hub_identity_rejects(
            conn,
            [
                protocol.IdentityReject(
                    table="tracks",
                    offered_pk=_PK_B,
                    survivor_pk=_PK_C,
                )
            ],
        )
        conn.commit()

        persisted = load_identity_remap(conn)
        assert _follow_remap(persisted, _PK_A) == _PK_C
        assert persisted[_PK_B] == _PK_C

        remap = effective_identity_remap(conn)
        assert _follow_remap(remap, _PK_A) == _PK_C
        held = sync_set.HeldKeys(conn)
        assert _PK_B in held.identity_losers
        assert _PK_C not in held.identity_losers
    finally:
        conn.close()
