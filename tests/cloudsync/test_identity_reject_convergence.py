"""CLOUDSYNC-07: hub-authority identity-collapse rejection convergence (issue #3057, #3100).

When hub and spoke elect opposite survivors for the same content identity,
the push response must name the hub survivor so the spoke can converge.
Reversed persisted remap rows must be deleted and one-round repair must
re-pull hub survivors and re-offer former local survivors.

[if] mirror-image elections diverge [then] one sync converges without digest mismatch, [else stop].
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.sync_hub import (
    capabilities,
    client,
    digest_diff,
    engine,
    protocol,
    sync_set,
    wire_version,
)
from apps.sync_hub.engine_identity import _follow_remap
from apps.sync_hub.engine_identity_map import (
    REMAP_TABLE,
    apply_hub_identity_rejects,
    effective_identity_remap,
    load_identity_remap,
    record_identity_remap,
)
from apps.sync_hub.transport import API_PREFIX, HttpTransport
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _DEV_B,
    _T0,
    _insert_playlist,
    _set_members,
    _TestClientTransport,
)
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _hub_app,
    _incoming_track,
    _insert_field,
    _insert_identified_track,
    _insert_location,
    _insert_vendor,
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


def _seed_track_children(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    location_id: str,
    playlist_id: str = "pl-mirror",
) -> None:
    """Children on one track PK for remap convergence tests."""
    _insert_field(
        conn,
        stable_id,
        field_name="bpm",
        value_json="128.0",
        updated_at=_STAMP,
        origin=_DEV_A,
    )
    _insert_location(
        conn,
        location_id=location_id,
        stable_id=stable_id,
        file_path=f"/Music/{stable_id}.mp3",
        updated_at=_STAMP,
        origin=_DEV_A,
    )
    _insert_vendor(
        conn,
        stable_id,
        vendor="rekordbox",
        vendor_id="rb-mirror",
        updated_at=_STAMP,
        origin=_DEV_A,
    )
    _insert_playlist(conn, playlist_id, name="mirror", updated_at=_STAMP, origin=_DEV_A)
    _set_members(conn, playlist_id, [stable_id], updated_at=_STAMP, origin=_DEV_A)


def _seed_peer_mirror_state(
    conn: sqlite3.Connection, *, loser: str, survivor: str
) -> None:
    """Both identity rows on one peer; shared children on the hub PK only."""
    for pk in (_PK_HUB, _PK_SPOKE):
        _insert_identified_track(
            conn,
            pk,
            title="mirror",
            content_hash=_HASH_A,
            updated_at=_STAMP,
            origin=_DEV_A,
        )
    _seed_track_children(
        conn,
        _PK_HUB,
        location_id="loc-mirror",
        playlist_id="pl-mirror",
    )
    record_identity_remap(conn, {}, loser, survivor)


@dataclass
class _RepairObservingTransport:
    """Count bundle repair pulls through a real TestClient transport."""

    inner: _TestClientTransport
    bundle_pulls: int = 0
    bundle_row_pks: tuple[tuple[str, ...], ...] = ()

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.inner.post(path, payload)

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        bundle = params.get("bundle_stable_ids")
        payload = self.inner.get(path, params)
        if bundle:
            self.bundle_pulls += 1
            self.bundle_row_pks += tuple(
                tuple(str(part) for part in row["pk"])
                for row in payload.get("rows", [])
            )
        return payload


def test_apply_hub_identity_rejects_validates_pks(tmp_path: Path) -> None:
    """[if] hub reject PKs are empty or equal [then] SyncProtocolError, [else stop]."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        with pytest.raises(protocol.SyncProtocolError, match="non-empty"):
            apply_hub_identity_rejects(
                conn,
                [
                    protocol.IdentityReject(
                        table="tracks",
                        offered_pk="",
                        survivor_pk=_PK_HUB,
                    )
                ],
            )
        with pytest.raises(protocol.SyncProtocolError, match="must differ"):
            apply_hub_identity_rejects(
                conn,
                [
                    protocol.IdentityReject(
                        table="tracks",
                        offered_pk=_PK_HUB,
                        survivor_pk=_PK_HUB,
                    )
                ],
            )
    finally:
        conn.close()


def test_http_transport_repeated_bundle_stable_ids() -> None:
    """[if] repair pull names multiple bundle ids [then] url repeats the key, [else stop]."""
    transport = HttpTransport("http://hub.example.invalid")
    url = transport._url(
        f"{API_PREFIX}/pull",
        {"machine_id": "spoke", "bundle_stable_ids": [_PK_HUB, _PK_SPOKE]},
    )
    assert "bundle_stable_ids=" in url
    assert url.count("bundle_stable_ids=") == 2


def test_spoke_apply_defers_drop_until_repair_offer(tmp_path: Path) -> None:
    """[if] hub row wins on pull [then] former survivor stays for repair offer, [else stop]."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _insert_identified_track(
            conn,
            _PK_SPOKE,
            title="local survivor",
            content_hash=_HASH_A,
            updated_at="2026-09-13T16:45:34.823926+00:00",
            origin=_DEV_A,
        )
        conn.commit()
        incoming = _incoming_track(
            conn,
            _PK_HUB,
            title="hub copy",
            updated_at=_STAMP,
            content_hash=_HASH_A,
        )
        result = engine.spoke_apply(conn, [incoming])
        assert result.accepted == 1
        assert _PK_SPOKE in _track_ids(conn)
        offer = engine.identity_repair_offer(conn, _PK_SPOKE)
        assert len(offer) == 1
        assert offer[0].pk == (_PK_SPOKE,)
    finally:
        conn.close()


def test_reversed_remap_row_deleted_on_hub_reject(tmp_path: Path) -> None:
    """[if] remap is reversed [then] hub reject remaps children, [else stop]."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        for pk in (_PK_HUB, _PK_SPOKE):
            _insert_identified_track(
                conn,
                pk,
                title="mirror",
                content_hash=_HASH_A,
                updated_at=_STAMP,
                origin=_DEV_A,
            )
        _seed_track_children(conn, _PK_SPOKE, location_id="loc-spoke")
        record_identity_remap(conn, {}, _PK_HUB, _PK_SPOKE)
        conn.commit()

        apply_hub_identity_rejects(
            conn,
            [
                protocol.IdentityReject(
                    table="tracks",
                    offered_pk=_PK_SPOKE,
                    survivor_pk=_PK_HUB,
                )
            ],
        )
        conn.commit()

        persisted = load_identity_remap(conn)
        assert persisted == {_PK_SPOKE: _PK_HUB}
        assert (
            conn.execute(
                f"SELECT 1 FROM {REMAP_TABLE} WHERE loser_pk = ?", (_PK_HUB,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT stable_id FROM track_fields WHERE field_name = 'bpm'"
            ).fetchone()[0]
            == _PK_HUB
        )
        assert (
            conn.execute(
                "SELECT stable_id FROM track_locations WHERE location_id = 'loc-spoke'"
            ).fetchone()[0]
            == _PK_HUB
        )
        assert (
            conn.execute(
                "SELECT stable_id FROM track_vendor_ids WHERE vendor = 'rekordbox'"
            ).fetchone()[0]
            == _PK_HUB
        )
        assert (
            conn.execute(
                "SELECT stable_id FROM playlist_memberships WHERE playlist_id = 'pl-mirror'"
            ).fetchone()[0]
            == _PK_HUB
        )
    finally:
        conn.close()


def test_spoke_apply_prefers_hub_pk_over_local_stamp(tmp_path: Path) -> None:
    """[if] hub row matches local identity [then] incoming hub PK wins on pull, [else stop]."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _insert_identified_track(
            conn,
            _PK_SPOKE,
            title="local newer",
            content_hash=_HASH_A,
            updated_at="2026-09-13T16:45:34.823926+00:00",
            origin=_DEV_A,
        )
        conn.commit()
        incoming = _incoming_track(
            conn,
            _PK_HUB,
            title="hub copy",
            updated_at=_STAMP,
            content_hash=_HASH_A,
        )
        result = engine.spoke_apply(conn, [incoming])
        assert result.accepted == 1
        assert load_identity_remap(conn)[_PK_SPOKE] == _PK_HUB
        assert _PK_HUB in _track_ids(conn)
    finally:
        conn.close()


def test_hub_apply_keeps_local_identity_election(tmp_path: Path) -> None:
    """[if] hub gets an identity conflict [then] local election stays, [else stop]."""
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
        assert result.identity_rejects[0].survivor_pk == _PK_HUB
    finally:
        conn.close()


def test_run_sync_repairs_identity_contradiction_from_pull(tmp_path: Path) -> None:
    """[if] pull contradicts local identity [then] repair runs once, [else stop]."""
    spoke = tmp_path / "spoke"
    hub_dir = tmp_path / "hub"

    with TestClient(_hub_app(hub_dir)) as http:
        transport = _TestClientTransport(http)
        initial = client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=transport,
            name="spoke",
        )

        spoke_conn = state_db.open_rw(client.state_db_path(spoke))
        try:
            for pk in (_PK_HUB, _PK_SPOKE):
                _insert_identified_track(
                    spoke_conn,
                    pk,
                    title="mirror",
                    content_hash=_HASH_A,
                    updated_at=_STAMP,
                    origin=_DEV_A,
                )
            record_identity_remap(spoke_conn, {}, _PK_HUB, _PK_SPOKE)
            watermark = engine.read_watermark(spoke_conn, initial.hub_machine_id)
            engine.write_watermark(
                spoke_conn,
                engine.Watermark(
                    peer=watermark.peer,
                    last_push_seq=engine.local_seq(spoke_conn),
                    last_pull_seq=watermark.last_pull_seq,
                    last_sync_at=watermark.last_sync_at,
                    peer_generation=watermark.peer_generation,
                ),
            )
            spoke_conn.commit()
        finally:
            spoke_conn.close()

        hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
        try:
            unrelated = _incoming_track(
                hub_conn,
                _PK_C,
                title="unrelated changelog row",
                updated_at=_STAMP,
                content_hash="c" * 64,
            )
            assert engine.hub_apply(hub_conn, [unrelated]).accepted == 1
            _seed_hub_track(hub_conn, _PK_HUB)
            _insert_identified_track(
                hub_conn,
                _PK_SPOKE,
                title="mirror",
                content_hash=_HASH_A,
                updated_at=_STAMP,
                origin=_DEV_A,
            )
            _seed_track_children(hub_conn, _PK_HUB, location_id="loc-mirror")
            record_identity_remap(hub_conn, {}, _PK_SPOKE, _PK_HUB)
            hub_conn.commit()
        finally:
            hub_conn.close()

        observing = _RepairObservingTransport(transport)
        result = client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=observing,
            name="spoke",
        )

    assert result.pushed == 0
    assert observing.bundle_pulls == 1
    assert (_PK_HUB,) in observing.bundle_row_pks
    assert (_PK_C,) not in observing.bundle_row_pks
    spoke_after = state_db.open_rw(client.state_db_path(spoke))
    try:
        assert load_identity_remap(spoke_after) == {_PK_SPOKE: _PK_HUB}
        assert _PK_SPOKE not in _track_ids(spoke_after)
        assert {_PK_HUB, _PK_C} == _track_ids(spoke_after)
    finally:
        spoke_after.close()


def test_reversed_remap_converges_in_one_sync(tmp_path: Path) -> None:
    """[if] remaps oppose [then] one sync converges and stays quiet, [else stop]."""
    spoke = tmp_path / "spoke"
    hub_dir = tmp_path / "hub"
    hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        _seed_peer_mirror_state(hub_conn, loser=_PK_SPOKE, survivor=_PK_HUB)
        hub_conn.commit()
    finally:
        hub_conn.close()

    spoke_conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _seed_peer_mirror_state(spoke_conn, loser=_PK_HUB, survivor=_PK_SPOKE)
        spoke_conn.commit()
    finally:
        spoke_conn.close()

    with TestClient(_hub_app(hub_dir)) as http:
        observing = _RepairObservingTransport(_TestClientTransport(http))
        result = client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=observing,
            name="spoke",
        )
        spoke_once = state_db.open_rw(client.state_db_path(spoke))
        hub_once = state_db.open_rw(client.state_db_path(hub_dir))
        try:
            assert digest_diff.sample_divergence(
                observing,
                spoke_once,
                result.machine_id,
                ("tracks",),
            ) == []
            for table in (
                "track_fields",
                "track_locations",
                "track_vendor_ids",
                "playlist_memberships",
            ):
                spoke_owners = spoke_once.execute(
                    f"SELECT stable_id FROM {table} ORDER BY stable_id"
                ).fetchall()
                hub_owners = hub_once.execute(
                    f"SELECT stable_id FROM {table} ORDER BY stable_id"
                ).fetchall()
                assert spoke_owners == hub_owners == [(_PK_HUB,)]
        finally:
            spoke_once.close()
            hub_once.close()
        bundle_pulls_after_first = observing.bundle_pulls
        quiet = client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=observing,
            name="spoke",
        )

    assert observing.bundle_pulls == 1
    assert observing.bundle_pulls == bundle_pulls_after_first
    assert not result.digest_inconclusive
    assert not quiet.digest_inconclusive

    spoke_after = state_db.open_rw(client.state_db_path(spoke))
    hub_after = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        assert load_identity_remap(spoke_after) == {_PK_SPOKE: _PK_HUB}
        assert (
            spoke_after.execute(
                f"SELECT 1 FROM {REMAP_TABLE} WHERE loser_pk = ?", (_PK_HUB,)
            ).fetchone()
            is None
        )
        local_digest = protocol.sync_digest(spoke_after)
        remote_digest = protocol.sync_digest(hub_after)
        assert local_digest.overall == remote_digest.overall
        for table in (
            "tracks",
            "track_fields",
            "track_locations",
            "track_vendor_ids",
            "playlist_memberships",
        ):
            assert local_digest.tables[table] == remote_digest.tables[table]
    finally:
        spoke_after.close()
        hub_after.close()

    assert quiet.pushed == 0
    assert quiet.pulled == 0
