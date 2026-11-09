"""The ``GET /cloudsync/overview`` payload: hydration counts and sync health.

Split out of ``routes/cloudsync.py`` (quality-gate ``file_size`` ratchet):
adding the sync-health block took that module past 600 lines, and the
boundary was already named by its own section comment. This module is a
LEAF -- it imports nothing from ``cloudsync.py``, which imports from it, so
the split stays acyclic.

Two readouts, deliberately different in kind:

* **Per-machine hydration** (:class:`MachineOverview`) -- how much of the
  library each machine is meant to hold and how much of that is not there
  yet. Derived from ``playlist_pins`` x ``playlist_memberships`` and real
  ``track_locations`` rows. Honest-denominator note (project house rule):
  these count DISTINCT tracks in playlists carrying an explicit pin for
  that machine and mode, NOT the whole library. A track outside every
  pinned playlist is governed by the machine's per-asset-kind default
  policy and is attributed to no bucket here, because the schema has no
  per-machine signal for "un-pinned but locally present".
* **This machine's sync position** (:class:`SyncHealthOut`) -- whether the
  fleet agrees, what is still owed to each peer, and how the last attempt
  ended. Whole-machine, not per-machine: a spoke can only report what IT
  knows, and reporting another machine's convergence from this DB would be
  a verdict rendered from an unmeasured subject.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from apps.shared.state import sync_stamp
from apps.sync_hub import health as sync_health

# ----- shared sqlite plumbing ----------------------------------------------


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True when ``name`` is a table in this connection's main schema."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def data_dir_for(conn: sqlite3.Connection) -> Path:
    """The data dir whose ``machine-id`` file owns this connection's writes.

    One source, the DB's own location, shared with every other writer in the
    repo: two data dirs on one host would mint two ``machine_id`` values
    under one hostname, and ``machines.name`` is UNIQUE.
    """
    try:
        return sync_stamp.data_dir_for_connection(conn)
    except sync_stamp.SyncStampError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "CLOUDSYNC_IDENTITY_ERROR",
                "message": str(exc),
            },
        ) from exc


# ----- schemas --------------------------------------------------------------


class MachineOverview(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_id: str
    name: str
    pinned_tracks: int
    cached_tracks: int
    stream_tracks: int
    unhydrated_pinned_count: int
    last_sync_at: str | None = None


class SyncPeerOut(BaseModel):
    """This machine's watermark against one peer, and what it still owes."""

    model_config = ConfigDict(frozen=True)

    peer: str
    last_push_seq: int
    last_pull_seq: int
    last_sync_at: str | None = None
    peer_generation: str | None = None
    pending_push_rows: int
    full_offer_pending: bool


class SyncRunOut(BaseModel):
    """The last recorded sync attempt. A failure carries zeros AND an error."""

    model_config = ConfigDict(frozen=True)

    started_at: str
    finished_at: str
    outcome: str
    hub_url: str
    consecutive_failures: int
    hub_machine_id: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
    pushed: int
    accepted: int
    rejected: int
    pulled: int
    applied: int
    rounds: int
    digest: str | None = None
    hub_restore_detected: bool
    digest_inconclusive: bool = False
    hub_quarantined: int | None = None
    quarantined_incoming: int = 0


class SyncHealthOut(BaseModel):
    """Field-for-field the shape :meth:`FleetHealth.to_wire` prints.

    ``GET /api/v1/cloudsync/overview``'s ``sync`` block is this readout,
    pinned by a test: agent-native parity is one computation with two
    renderers, not two renderers that happen to agree today.
    """

    model_config = ConfigDict(frozen=True)

    verdict: str
    local_seq: int
    #: None, not 0, on a machine with no peer yet: it owes its whole library
    #: to a hub it has never met, and 0 would read as converged.
    pending_push_rows: int | None = None
    full_offer_pending: bool
    digest_mismatch: bool
    #: Rows on THIS machine that are not in the sync set: a stored stamp
    #: that cannot be ordered, or a row that REFERENCES one. The same count
    #: the digest excluded, not a second reading of it. Non-zero means one
    #: operator command (``python -m apps.shared.state.normalize_stamps
    #: --live``) stands between those rows and every other machine.
    quarantined_rows: int = 0
    peers: list[SyncPeerOut]
    last_run: SyncRunOut | None = None


class OverviewOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_tracks: int
    machines: list[MachineOverview]
    #: None ONLY when this DB predates schema v7 and has no sync tables at
    #: all. Never None as a stand-in for "nothing has synced yet" -- that
    #: reading is ``verdict: never_synced``, which is a measurement.
    sync: SyncHealthOut | None = None


# ----- builders -------------------------------------------------------------


def machine_overview_row(conn: sqlite3.Connection, machine_id: str, name: str) -> MachineOverview:
    """Pin counts and hydration for one machine. See the docstring's note."""

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
    if table_exists(conn, "sync_state"):
        last_sync_row = conn.execute(
            "SELECT last_sync_at FROM sync_state WHERE peer = ?",
            (machine_id,),
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


def sync_health_block(conn: sqlite3.Connection) -> SyncHealthOut:
    """The ``sync`` block, straight off :func:`apps.sync_hub.health.fleet_health`.

    A :class:`apps.sync_hub.health.SyncHealthError` propagates as a 500
    rather than being folded into an empty readout: "this machine has never
    synced" and "this machine's record of syncing is unreadable" are
    different facts, and only one of them is fixed by waiting
    (``(private path)`` -- a tool that cannot measure must
    report UNKNOWN, never a verdict).
    """
    try:
        fleet = sync_health.fleet_health(conn, data_dir_for(conn))
    except sync_health.SyncHealthError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "CLOUDSYNC_HEALTH_UNREADABLE",
                "message": str(exc),
            },
        ) from exc
    return SyncHealthOut.model_validate(fleet.to_wire())


def build_overview(conn: sqlite3.Connection) -> OverviewOut:
    """The whole ``GET /cloudsync/overview`` body for one connection."""
    total_row = conn.execute("SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL").fetchone()
    total_tracks = int(total_row[0]) if total_row else 0

    if not table_exists(conn, "machines") or not table_exists(conn, "playlist_pins"):
        # Pre-v7 DB: the sync tables do not exist either (one migration
        # created all of them), so there is nothing to measure here and
        # nothing to render a verdict about.
        return OverviewOut(total_tracks=total_tracks, machines=[], sync=None)

    machine_rows = conn.execute("SELECT machine_id, name FROM machines ORDER BY name").fetchall()
    machines = [machine_overview_row(conn, machine_id, name) for machine_id, name in machine_rows]
    return OverviewOut(
        total_tracks=total_tracks,
        machines=machines,
        sync=sync_health_block(conn),
    )


__all__ = [
    "MachineOverview",
    "OverviewOut",
    "SyncHealthOut",
    "SyncPeerOut",
    "SyncRunOut",
    "build_overview",
    "data_dir_for",
    "machine_overview_row",
    "sync_health_block",
    "table_exists",
]
