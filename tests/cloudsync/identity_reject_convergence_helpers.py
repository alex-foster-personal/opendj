"""Shared helpers for the test_identity_reject_convergence test modules (split from test_identity_reject_convergence.py)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.sync_hub import (
    engine,
    protocol,
)
from apps.sync_hub.engine_identity_map import (
    record_identity_remap,
)
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T0,
    _insert_playlist,
    _set_members,
    _TestClientTransport,
)
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _incoming_track,
    _insert_field,
    _insert_identified_track,
    _insert_location,
    _insert_vendor,
    _values,
)

_PK_HUB = "a00000000000000000000000000000000000000001"
_PK_SPOKE = "b00000000000000000000000000000000000000002"
_PK_A = "c00000000000000000000000000000000000000003"
_PK_B = "d00000000000000000000000000000000000000004"
_PK_C = "e00000000000000000000000000000000000000005"
_STAMP = "2026-09-12T16:45:34.823926+00:00"


def _spoke_row_values(conn: sqlite3.Connection, stable_id: str, **overrides: object) -> dict[str, Any]:
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


def _seed_peer_mirror_state(conn: sqlite3.Connection, *, loser: str, survivor: str) -> None:
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

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.inner.post(path, payload)

    def get(self, path: str, params: Mapping[str, str | Sequence[str]]) -> dict[str, Any]:
        bundle = params.get("bundle_stable_ids")
        payload = self.inner.get(path, params)
        if bundle:
            self.bundle_pulls += 1
            self.bundle_row_pks += tuple(tuple(str(part) for part in row["pk"]) for row in payload.get("rows", []))
        return payload


class _RenamesBetweenChunks:
    """A hub link that, after the first loser-bundle read, lets the spoke push a rename."""

    def __init__(self, inner: _TestClientTransport, rename: Any) -> None:
        self._inner = inner
        self._rename = rename
        self.bundle_reads = 0
        self.fired = False

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._inner.post(path, payload)

    def get(self, path: str, params: Mapping[str, str | Sequence[str]]) -> dict[str, Any]:
        answer = self._inner.get(path, params)
        if path.endswith("/pull") and params.get("bundle_stable_ids"):
            self.bundle_reads += 1
            if self.bundle_reads == 1:
                self._rename()
                self.fired = True
        return answer
