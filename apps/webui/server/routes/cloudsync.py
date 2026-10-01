"""CloudSync config surface: fleet machines, per-machine asset policy,
playlist pins, and a per-machine hydration overview.

Contract: ``specs/cloudsync-spec.md`` section D5, ``specs/design_decision_06.md``
(policy semantics), schema ``specs/design_decision_05.md`` (migration v6 --
``machines``, ``sync_policies``, ``playlist_pins``, ``sync_state`` tables in
``apps/shared/state/schema.py``). Self-contained router: it does not use the
``StateBackend`` abstraction (CAT-05 hotspot) -- these tables are new and not
part of that surface, so this router talks to the schema-owned sqlite tables
directly, mirroring ``routes/smartlists.py``.

Endpoints (full agent parity -- every UI action below maps 1:1 to one of
these; nothing here is UI-only):

  * ``GET  /cloudsync/machines``       -- fleet roster. Also self-registers
    (upsert + heartbeat) the machine THIS daemon process is running on via
    :func:`apps.shared.state.machine_identity.register_machine`, so the
    matrix is populated without waiting on the hub-sync daemon (not yet
    built) to do it first. Idempotent.
  * ``GET  /cloudsync/policies``       -- ``sync_policies`` rows, optionally
    filtered by ``machine_id``.
  * ``PUT  /cloudsync/policies``       -- upsert one (machine_id, asset_kind)
    cell through :func:`apps.sync_hub.policy_store.apply_proposal`: gated
    (409 ``POLICY_VIOLATION`` with the violations, nothing written), then
    stamped with ``updated_at`` + ``origin_device_id`` (this server's own
    machine id, NOT the target machine) per D5 and logged to
    ``local_changelog`` in the same transaction.
  * ``GET  /cloudsync/playlist-pins``  -- ``playlist_pins`` rows joined to
    ``playlists.name``, optionally filtered by ``machine_id``.
  * ``PUT  /cloudsync/playlist-pins``  -- upsert one (machine_id,
    playlist_id) pin, through the same gate and write path.
  * ``GET  /cloudsync/overview``       -- per-machine track counts derived
    from ``playlist_pins`` x ``playlist_memberships``, an unhydrated-pinned
    count, and ``last_sync_at`` from the machine-local ``sync_state`` table.

Both PUTs write through :mod:`apps.sync_hub.policy_store` (the policy gate:
409 on an error the change introduces or leaves on the cell it names).
Validate, plan, apply, the DELETE tombstones and ``/data-classes`` live in
``routes/cloudsync_policy.py``.

Honest-denominator note (project house rule): ``pinned_tracks`` /
``cached_tracks`` / ``stream_tracks`` count DISTINCT tracks in playlists that
carry an explicit pin for that machine and mode -- NOT the whole library.
A track outside every pinned playlist is governed only by the machine's
per-asset-kind default policy (``sync_policies``) and is not attributed to
any bucket here. ``unhydrated_pinned_count`` counts a machine's pinned tracks
with no ``track_locations`` row ON THAT MACHINE (``machine_id`` matches,
``kind='local' AND available=1``, not tombstoned): another machine's local
copy does not make this one hydrated. A location with a NULL ``machine_id``
belongs to nobody until ``sync_stamp.backfill_local_machine_id`` claims it.

Registered in ``app.py`` by the sync-engine lane (hotspot, not edited here):
``app.include_router(cloudsync_routes.router, prefix=api_prefix)``.

Identity note (round 2 finding N1a, round 3): this router's machine identity
comes from the CONNECTION -- ``sync_stamp.data_dir_for_connection`` -- not
from ``app.state.data_dir``. One machine must have exactly one identity: two
data dirs on one host mint two ``machine_id`` values under one hostname, and
``machines.name`` is UNIQUE, so the second registration is a 409 rather than
a second machine. Deriving from the DB the writes land in is what makes this
router's ``origin_device_id`` the same id ``StateWriter``, ``provenance`` and
``locations`` stamp with.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.shared.state.machine_identity import (
    MachineIdentityError,
    register_machine,
)
from apps.sync_hub import config as sync_config
from apps.sync_hub.policy_rules import PinCell, PolicyCell
from apps.sync_hub.policy_store import empty_proposal
from apps.webui.server.cloudsync_policy_http import (
    apply_policy_or_raise,
    author_or_raise,
    policy_write_responses,
)

router = APIRouter(prefix="/cloudsync", tags=["cloudsync"])

AssetKind = Literal[
    "audio",
    "stem_bundle",
    "anlz_cache",
    "vocal_cache",
    "lyrics_cache",
    "karaoke_words",
]
SyncMode = Literal["pinned", "cached", "stream", "excluded"]


# ----- connection + identity plumbing --------------------------------------

def _db_path(request: Request) -> Path:
    return Path(
        getattr(request.app.state, "state_db_path", "data/state/state.db")
    )


def _data_dir(conn: sqlite3.Connection) -> Path:
    """The data dir whose ``machine-id`` file owns this connection's writes.

    See the identity note in the module docstring: one source, the DB's own
    location, shared with every other writer in the repo.
    """
    try:
        return sync_stamp.data_dir_for_connection(conn)
    except sync_stamp.SyncStampError as exc:
        raise HTTPException(status_code=500, detail={
            "code": "CLOUDSYNC_IDENTITY_ERROR", "message": str(exc),
        }) from exc


def _unavailable(db_path: Path) -> HTTPException:
    return HTTPException(status_code=503, detail={
        "code": "CLOUDSYNC_DB_UNAVAILABLE",
        "message": (
            f"state DB not found at {db_path}; run "
            "`python -m apps.shared.state.cli init` first."
        ),
    })


def get_cloudsync_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Read-only sqlite conn (query_only guard).

    ``check_same_thread=False``: FastAPI runs sync dependencies on a
    threadpool, so a conn created here with the sqlite default raises
    under load (same reasoning as ``routes/smartlists.py``).
    """
    db_path = _db_path(request)
    if not db_path.exists():
        raise _unavailable(db_path)
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, isolation_level=None,
        check_same_thread=False,
    )
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def get_cloudsync_write_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Writable conn with migrations applied up front.

    Applying migrations here (rather than assuming ``SqliteBackend`` already
    ran them) means this router's own tables exist even if nothing else has
    opened the DB read-write yet -- CLOUDSYNC's schema authority (D2) is
    ``apps.shared.state.schema``, so calling it directly is the correct
    dependency, not a shortcut around it.
    """
    db_path = _db_path(request)
    if not db_path.exists():
        raise _unavailable(db_path)
    conn = state_db.open_rw(db_path, apply_schema=True, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone()
    return row is not None


# ----------------------------------------------------------- schemas

class MachineOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_id: str
    name: str
    platform: Literal["macos", "windows", "linux"]
    is_hub: bool
    data_root: str | None = None
    first_seen: str
    last_seen: str


class SyncPolicyOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_id: str
    asset_kind: AssetKind
    mode: SyncMode
    cache_budget_mb: int | None = None
    updated_at: str | None = None
    origin_device_id: str | None = None


class SyncPolicyPut(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_id: str
    asset_kind: AssetKind
    mode: SyncMode
    cache_budget_mb: int | None = Field(default=None, ge=0)


class PlaylistPinOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_id: str
    playlist_id: str
    playlist_name: str | None = None
    mode: SyncMode
    updated_at: str | None = None
    origin_device_id: str | None = None


class PlaylistPinPut(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_id: str
    playlist_id: str
    mode: SyncMode


class MachineOverview(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_id: str
    name: str
    pinned_tracks: int
    cached_tracks: int
    stream_tracks: int
    unhydrated_pinned_count: int
    last_sync_at: str | None = None


class OverviewOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_tracks: int
    machines: list[MachineOverview]


# ----------------------------------------------------------- helpers

def _row_to_machine(row: tuple) -> MachineOut:
    return MachineOut(
        machine_id=row[0], name=row[1], platform=row[2],
        is_hub=bool(row[3]), data_root=row[4],
        first_seen=row[5], last_seen=row[6],
    )


def _row_to_policy(row: tuple) -> SyncPolicyOut:
    return SyncPolicyOut(
        machine_id=row[0], asset_kind=row[1], mode=row[2],
        cache_budget_mb=row[3], updated_at=row[4], origin_device_id=row[5],
    )


def _row_to_pin(row: tuple) -> PlaylistPinOut:
    return PlaylistPinOut(
        machine_id=row[0], playlist_id=row[1], playlist_name=row[2],
        mode=row[3], updated_at=row[4], origin_device_id=row[5],
    )


# ----------------------------------------------------------- machines
#
# ``Depends(...)`` in a parameter default below (every route in this file) is
# the standard FastAPI DI idiom; ruff's B008 rule flags any function call in
# a default and does not special-case it. Each occurrence carries a targeted
# per-line lint suppression rather than a repo-wide ruff config change: the
# identical pattern already sits unsuppressed as accepted debt in 23 other
# route modules (81 hits of this same rule total, round 1 quality-gate
# output), so a config change belongs to whichever lane owns pyproject.toml's
# ruff block, not this one.

@router.get("/machines", response_model=list[MachineOut], responses={500: {
    "description": "CLOUDSYNC_IDENTITY_ERROR or CLOUDSYNC_CONFIG_INVALID "
    "(cloudsync-config.json is malformed, so the configured name is unknown)",
}})
def list_machines(
    conn: sqlite3.Connection = Depends(get_cloudsync_write_conn),  # noqa: B008
) -> list[MachineOut]:
    # Register under the CONFIGURED name, the one Sync now and the scheduler
    # use; the hostname default would rename this machine back (and collide
    # with a same-host hub's row on UNIQUE machines.name).
    data_dir = _data_dir(conn)
    try:
        register_machine(
            conn, data_dir=data_dir, name=sync_config.configured_machine_name(data_dir)
        )
    except sync_config.CloudSyncConfigError as exc:
        raise HTTPException(status_code=500, detail={
            "code": "CLOUDSYNC_CONFIG_INVALID", "message": str(exc),
        }) from exc
    except MachineIdentityError as exc:
        raise HTTPException(status_code=500, detail={
            "code": "CLOUDSYNC_IDENTITY_ERROR", "message": str(exc),
        }) from exc
    rows = conn.execute(
        "SELECT machine_id, name, platform, is_hub, data_root, "
        "first_seen, last_seen FROM machines ORDER BY name"
    ).fetchall()
    return [_row_to_machine(r) for r in rows]


# ----------------------------------------------------------- policies

@router.get("/policies", response_model=list[SyncPolicyOut])
def list_policies(
    machine_id: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_cloudsync_conn),  # noqa: B008
) -> list[SyncPolicyOut]:
    if not _table_exists(conn, "sync_policies"):
        return []
    sql = (
        "SELECT machine_id, asset_kind, mode, cache_budget_mb, updated_at, "
        "origin_device_id FROM sync_policies WHERE deleted_at IS NULL"
    )
    params: tuple = ()
    if machine_id is not None:
        sql += " AND machine_id = ?"
        params = (machine_id,)
    sql += " ORDER BY machine_id, asset_kind"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_policy(r) for r in rows]


@router.put(
    "/policies",
    response_model=SyncPolicyOut,
    response_description="The stored cell",
    responses=policy_write_responses("MACHINE_NOT_FOUND"),
)
def put_policy(
    body: SyncPolicyPut,
    conn: sqlite3.Connection = Depends(get_cloudsync_write_conn),  # noqa: B008
) -> SyncPolicyOut:
    """Upsert one policy cell through the policy gate (``policy set --live`` twin).

    ``apps.sync_hub.policy_store`` stamps the row and its ``local_changelog``
    entry in one transaction (round 2 finding N1a: a write with no changelog
    entry is never offered to the hub) and answers 409 when the change
    introduces an error-severity violation, writing nothing.
    """
    cell = PolicyCell(body.machine_id, body.asset_kind, body.mode, body.cache_budget_mb)
    proposed = replace(empty_proposal(author_or_raise(conn)), policies=(cell,))
    apply_policy_or_raise(conn, proposed, live=True)
    row = conn.execute(
        "SELECT machine_id, asset_kind, mode, cache_budget_mb, updated_at, "
        "origin_device_id FROM sync_policies WHERE machine_id = ? AND asset_kind = ?",
        (body.machine_id, body.asset_kind),
    ).fetchone()
    return _row_to_policy(row)


# ----------------------------------------------------------- playlist pins

@router.get("/playlist-pins", response_model=list[PlaylistPinOut])
def list_playlist_pins(
    machine_id: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_cloudsync_conn),  # noqa: B008
) -> list[PlaylistPinOut]:
    if not _table_exists(conn, "playlist_pins"):
        return []
    sql = (
        "SELECT pp.machine_id, pp.playlist_id, p.name, pp.mode, "
        "pp.updated_at, pp.origin_device_id "
        "FROM playlist_pins pp LEFT JOIN playlists p "
        "ON p.playlist_id = pp.playlist_id "
        "WHERE pp.deleted_at IS NULL"
    )
    params: tuple = ()
    if machine_id is not None:
        sql += " AND pp.machine_id = ?"
        params = (machine_id,)
    sql += " ORDER BY pp.machine_id, p.name"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_pin(r) for r in rows]


@router.put(
    "/playlist-pins",
    response_model=PlaylistPinOut,
    response_description="The stored pin",
    responses=policy_write_responses("MACHINE_NOT_FOUND", "PLAYLIST_NOT_FOUND"),
)
def put_playlist_pin(
    body: PlaylistPinPut,
    conn: sqlite3.Connection = Depends(get_cloudsync_write_conn),  # noqa: B008
) -> PlaylistPinOut:
    """Upsert one pin through the policy gate. See :func:`put_policy`."""
    pin = PinCell(body.machine_id, body.playlist_id, body.mode)
    proposed = replace(empty_proposal(author_or_raise(conn)), pins=(pin,))
    apply_policy_or_raise(conn, proposed, live=True)
    row = conn.execute(
        "SELECT pp.machine_id, pp.playlist_id, p.name, pp.mode, pp.updated_at, "
        "pp.origin_device_id FROM playlist_pins pp LEFT JOIN playlists p "
        "ON p.playlist_id = pp.playlist_id "
        "WHERE pp.machine_id = ? AND pp.playlist_id = ?",
        (body.machine_id, body.playlist_id),
    ).fetchone()
    return _row_to_pin(row)


# ----------------------------------------------------------- overview

def _machine_overview_row(conn: sqlite3.Connection, machine_id: str, name: str) -> MachineOverview:
    def _pin_count(mode: str) -> int:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT pm.stable_id)
            FROM playlist_pins pp
            JOIN playlist_memberships pm ON pm.playlist_id = pp.playlist_id
            WHERE pp.machine_id = ? AND pp.mode = ? AND pp.deleted_at IS NULL
              AND pm.deleted_at IS NULL
            """,
            (machine_id, mode),
        ).fetchone()
        return int(row[0]) if row else 0

    unhydrated_row = conn.execute(
        """
        SELECT COUNT(DISTINCT pm.stable_id)
        FROM playlist_pins pp
        JOIN playlist_memberships pm ON pm.playlist_id = pp.playlist_id
        WHERE pp.machine_id = ? AND pp.mode = 'pinned' AND pp.deleted_at IS NULL
          AND pm.deleted_at IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM track_locations tl
            WHERE tl.stable_id = pm.stable_id AND tl.kind = 'local'
              AND tl.machine_id = pp.machine_id
              AND tl.available = 1 AND tl.deleted_at IS NULL
          )
        """,
        (machine_id,),
    ).fetchone()

    last_sync_row = None
    if _table_exists(conn, "sync_state"):
        last_sync_row = conn.execute(
            "SELECT last_sync_at FROM sync_state WHERE peer = ?", (machine_id,),
        ).fetchone()

    return MachineOverview(
        machine_id=machine_id,
        name=name,
        pinned_tracks=_pin_count("pinned"),
        cached_tracks=_pin_count("cached"),
        stream_tracks=_pin_count("stream"),
        unhydrated_pinned_count=int(unhydrated_row[0]) if unhydrated_row else 0,
        last_sync_at=last_sync_row[0] if last_sync_row else None,
    )


@router.get("/overview", response_model=OverviewOut)
def get_overview(
    conn: sqlite3.Connection = Depends(get_cloudsync_conn),  # noqa: B008
) -> OverviewOut:
    total_row = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL"
    ).fetchone()
    total_tracks = int(total_row[0]) if total_row else 0

    if not _table_exists(conn, "machines") or not _table_exists(conn, "playlist_pins"):
        return OverviewOut(total_tracks=total_tracks, machines=[])

    machine_rows = conn.execute(
        "SELECT machine_id, name FROM machines ORDER BY name"
    ).fetchall()
    machines = [
        _machine_overview_row(conn, machine_id, name)
        for machine_id, name in machine_rows
    ]
    return OverviewOut(total_tracks=total_tracks, machines=machines)


__all__ = [
    "AssetKind",
    "MachineOut",
    "MachineOverview",
    "OverviewOut",
    "PlaylistPinOut",
    "PlaylistPinPut",
    "SyncMode",
    "SyncPolicyOut",
    "SyncPolicyPut",
    "router",
]
