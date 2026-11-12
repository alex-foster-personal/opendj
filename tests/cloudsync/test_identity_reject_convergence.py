"""CLOUDSYNC-07: hub-authority identity-collapse rejection convergence (issue #3057, #3100).

When hub and spoke elect opposite survivors for the same content identity,
the push response must name the hub survivor so the spoke can converge.
Reversed persisted remap rows must be deleted and one-round repair must
re-pull hub survivors and re-offer former local survivors.

[if] mirror-image elections diverge [then] one sync converges without digest mismatch, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.sync_hub import (
    capabilities,
    client,
    engine,
    protocol,
    sync_set,
    wire_version,
)
from apps.sync_hub.engine_identity import _follow_remap
from apps.sync_hub.engine_identity_map import (
    apply_hub_identity_rejects,
    effective_identity_remap,
    load_identity_remap,
    record_identity_remap,
)
from apps.sync_hub.transport import API_PREFIX, HttpTransport
from tests.cloudsync.identity_reject_convergence_helpers import (
    _PK_A,
    _PK_B,
    _PK_C,
    _PK_HUB,
    _PK_SPOKE,
    _STAMP,
    _RenamesBetweenChunks,
    _seed_hub_track,
    _spoke_row_values,
)
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _DEV_B,
    _T0,
    _T1,
    _insert_playlist,
    _log,
    _open,
    _set_members,
    _sync,
    _TestClientTransport,
)
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _hub_app,
    _incoming_track,
    _insert_field,
    _insert_identified_track,
    _insert_location,
    _open_hub,
    _track_ids,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")


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
        field_owner = spoke_after.execute("SELECT stable_id FROM track_fields WHERE field_name = 'bpm'").fetchone()
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
            assert sync_set.row_reason("tracks", columns, row, spec, held) == sync_set.IDENTITY_DUP_REASON
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
        assert held.is_identity_loser(_PK_B)
        assert not held.is_identity_loser(_PK_C)
    finally:
        conn.close()


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


def test_library_scale_bundle_ids_are_chunked_under_8kib() -> None:
    """[if] 161 loser ids in one GET exceed 8KiB [then] fail, [else stop]."""
    from apps.sync_hub.client_recovery import BUNDLE_STABLE_IDS_PER_GET, bundle_stable_id_chunks

    losers = [f"{index:040x}" for index in range(161)]
    transport = HttpTransport("http://hub.example.invalid")
    envelope = {
        "machine_id": "spoke",
        "capabilities": list(capabilities.THIS_BUILD),
    }
    whole = transport._url(
        f"{API_PREFIX}/pull",
        {**envelope, "bundle_stable_ids": losers},
    )
    assert len(whole.encode()) > 8190, "control: the unchunked query must overflow nginx"
    chunks = bundle_stable_id_chunks(losers)
    assert sum(len(chunk) for chunk in chunks) == 161
    assert all(len(chunk) <= BUNDLE_STABLE_IDS_PER_GET for chunk in chunks)
    for chunk in chunks:
        url = transport._url(
            f"{API_PREFIX}/pull",
            {**envelope, "bundle_stable_ids": list(chunk)},
        )
        assert len(url.encode()) < 4096, url


def test_a_playlist_shared_across_bundle_chunks_is_kept_once(hub: _TestClientTransport, spoke_b: Path) -> None:
    """[if] each bundle chunk adds its own copy of a shared playlist [then] fail, [else stop].

    Real hub, real ``GET /pull``: one more loser than a chunk holds, all in
    one playlist, so two chunks each bring that playlist's whole membership.
    """
    from apps.shared.state import machine_identity
    from apps.sync_hub import client_recovery

    losers = [f"{index:040x}" for index in range(client_recovery.BUNDLE_STABLE_IDS_PER_GET + 1)]
    playlist = "f" * 40
    conn = _open(spoke_b)
    try:
        for index, stable_id in enumerate(losers):
            _insert_identified_track(
                conn,
                stable_id,
                title=f"loser {index}",
                updated_at=_T0,
                origin=_DEV_B,
                content_hash=f"{index:064x}",
            )
        _insert_playlist(conn, playlist, name="shared", updated_at=_T0, origin=_DEV_B)
        _set_members(conn, playlist, tuple(losers), updated_at=_T0, origin=_DEV_B)
        conn.commit()
    finally:
        conn.close()
    _sync(spoke_b, hub, "spoke-b")
    machine_id = machine_identity.get_or_create_machine_id(spoke_b)
    assert len(client_recovery.bundle_stable_id_chunks(losers)) == 2, "control: the losers must span two chunks"

    bundle = client_recovery._loser_bundles(hub, machine_id, losers)

    keys = [(row["table"], tuple(row["pk"])) for row in bundle["rows"]]
    assert keys.count(("playlists", (playlist,))) == 1, (
        f"the shared playlist arrived {keys.count(('playlists', (playlist,)))} times"
    )
    assert sorted(key[1][0] for key in keys if key[0] == "tracks") == sorted(losers), (
        "a loser's own row was dropped or duplicated"
    )
    assert len(keys) == len(set(keys)), "a bundle row arrived twice"


def test_a_playlist_that_changes_between_bundle_chunks_is_refused(hub: _TestClientTransport, spoke_b: Path) -> None:
    """[if] two chunks disagree on a shared row and the first copy is kept [then] fail, [else stop].

    Real hub, real ``GET /pull``, real push: the spoke renames the shared
    playlist and syncs it between the two chunk reads, so the second chunk
    carries a different playlist row than the first.
    """
    from apps.shared.state import machine_identity
    from apps.sync_hub import client_recovery

    losers = [f"{index:040x}" for index in range(client_recovery.BUNDLE_STABLE_IDS_PER_GET + 1)]
    playlist = "f" * 40
    conn = _open(spoke_b)
    try:
        for index, stable_id in enumerate(losers):
            _insert_identified_track(
                conn,
                stable_id,
                title=f"loser {index}",
                updated_at=_T0,
                origin=_DEV_B,
                content_hash=f"{index:064x}",
            )
        _insert_playlist(conn, playlist, name="shared", updated_at=_T0, origin=_DEV_B)
        _set_members(conn, playlist, tuple(losers), updated_at=_T0, origin=_DEV_B)
        conn.commit()
    finally:
        conn.close()
    _sync(spoke_b, hub, "spoke-b")
    machine_id = machine_identity.get_or_create_machine_id(spoke_b)

    def rename_and_push() -> None:
        renaming = _open(spoke_b)
        try:
            stamped = _log(renaming, "playlists", (playlist,), _DEV_B, _T1)
            renaming.execute(
                "UPDATE playlists SET name = ?, updated_at = ? WHERE playlist_id = ?",
                ("renamed mid-read", stamped, playlist),
            )
            renaming.commit()
        finally:
            renaming.close()
        _sync(spoke_b, hub, "spoke-b")

    moving = _RenamesBetweenChunks(hub, rename_and_push)
    with pytest.raises(client_recovery._BundleChunksDisagree, match="chunks disagree on playlists"):
        client_recovery._loser_bundles(moving, machine_id, losers)

    assert moving.fired, "control: the rename never landed between the chunk reads"
    assert moving.bundle_reads == 2, "control: the losers did not span two chunk reads"


def test_bundle_chunks_that_disagree_are_a_moved_proof_the_scheduler_retries(
    hub: _TestClientTransport, spoke_a: Path, spoke_b: Path
) -> None:
    """[if] chunks that disagree halt or crash the sync instead of retrying [then] fail, [else stop].

    End to end over the real hub: 33 stale remaps, so the losers' bundle
    takes two reads, and the shared playlist is renamed and pushed between
    them. A hub that moved mid-proof is concurrency, so ``run_sync`` must
    raise ``SyncStillMoving`` with every remap kept, and the next quiet
    sync must retire them all.
    """
    from apps.shared.state.ids import stable_id
    from apps.sync_hub import client_recovery
    from apps.sync_hub.engine_identity_map import prepare_spoke_identity

    count = client_recovery.BUNDLE_STABLE_IDS_PER_GET + 1
    pairs = [(stable_id(isrc=f"XXFAK27{index:05d}")[0], f"{index + 1:040x}") for index in range(count)]
    playlist = "f" * 40
    conn = _open(spoke_b)
    try:
        for index, (loser, survivor) in enumerate(pairs):
            _insert_identified_track(
                conn,
                survivor,
                title=f"survivor {index}",
                updated_at=_T0,
                origin=_DEV_B,
                content_hash=f"{index:064x}",
            )
            _insert_identified_track(
                conn,
                loser,
                title=f"loser {index}",
                updated_at=_T0,
                origin=_DEV_B,
                isrc=f"XXFAK27{index:05d}",
                tier="isrc",
            )
        _insert_playlist(conn, playlist, name="shared", updated_at=_T0, origin=_DEV_B)
        members = tuple(track for pair in pairs for track in pair)
        _set_members(conn, playlist, members, updated_at=_T0, origin=_DEV_B)
        conn.commit()
    finally:
        conn.close()
    _sync(spoke_b, hub, "spoke-b")
    _sync(spoke_a, hub, "spoke-a")
    conn = _open(spoke_a)
    try:
        for loser, survivor in pairs:
            record_identity_remap(conn, {}, loser, survivor)
        conn.commit()
        prepare_spoke_identity(conn)
        conn.executemany("DELETE FROM tracks WHERE stable_id = ?", [(loser,) for loser, _ in pairs])
        conn.commit()
    finally:
        conn.close()

    def rename_and_push() -> None:
        renaming = _open(spoke_b)
        try:
            stamped = _log(renaming, "playlists", (playlist,), _DEV_B, _T1)
            renaming.execute(
                "UPDATE playlists SET name = ?, updated_at = ? WHERE playlist_id = ?",
                ("renamed mid-read", stamped, playlist),
            )
            renaming.commit()
        finally:
            renaming.close()
        _sync(spoke_b, hub, "spoke-b")

    moving = _RenamesBetweenChunks(hub, rename_and_push)
    with pytest.raises(client.SyncStillMoving, match="chunks disagree on playlists"):
        client.run_sync(spoke_a, "http://hub.invalid", transport=moving, name="spoke-a")

    assert moving.fired, "control: the rename never landed between the chunk reads"
    assert moving.bundle_reads == 2, "control: the losers did not span two chunk reads"
    kept = _open(spoke_a)
    try:
        assert set(load_identity_remap(kept)) == {loser for loser, _ in pairs}, "a remap was retired"
    finally:
        kept.close()

    healed = _sync(spoke_a, hub, "spoke-a")

    assert healed.stale_identity_remaps_dropped == count, "the quiet sync did not retire them"
