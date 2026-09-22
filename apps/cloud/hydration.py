"""Policy-driven playback resolution, cache eviction, push-then-delete.

Implements the READ PATH and WRITE PATH of ADR 06
(``specs/design_decision_06.md``) over the v6 policy tables
(``specs/design_decision_05.md``).

The READ PATH -- cache layout, policy resolution, and
:func:`~apps.cloud.hydration_core.resolve_playback_source` -- moved to
:mod:`apps.cloud.hydration_core` (quality-gate file_size ratchet, round 4;
see that module's docstring for the ADR order). This module keeps the WRITE
PATH (push-then-delete, below) and re-exports the read path, so every caller
that already does ``from apps.cloud import hydration`` and reads
``hydration.X`` keeps the surface it had.
"""
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apps.shared.state import locations as state_locations
from apps.shared.state import sync_stamp

from . import policy as cloud_policy
from . import transfer_status
from .asset_store import (
    AssetS3Client,
    AssetStoreError,
    fetch_asset,
    fetch_presigned_asset,
    object_exists,
    push_asset,
)
from .config import CloudConfig
from .eviction import BYTES_PER_MB, EvictionResult, HydrationError, evict_cache
from .hydration_core import (
    ASSET_KINDS,
    MODE_STRENGTH,
    LocalAction,
    PlaybackSource,
    PolicyMode,
    PolicyResolution,
    PolicySource,
    cache_path,
    resolve_playback_source,
    resolve_policy,
    touch_cache_entry,
)

#: The one synced table this module writes. Named rather than repeated so the
#: ``local_changelog`` entry and the ``INSERT`` can never name two tables.
_LOCATIONS_TABLE: str = "track_locations"

if TYPE_CHECKING:
    from apps.engine_core.jobs.store import JobStore


# --- download path (hydration fetch + transfer ledger) -----------------------


def fetch_asset_for_hydration(
    cfg: CloudConfig | None,
    s3: AssetS3Client | None,
    content_hash: str,
    dest: Path,
    *,
    stable_id: str,
    bytes_total: int | None = None,
    _pressure_payload: Mapping[str, Any] | None = None,
    _ui_mirror: Mapping[str, Any] | None = None,
    presigned_url: str | None = None,
) -> Path:
    """Download an asset for hydration with transfer_status progress.

    Pool acquisition happens inside :func:`~apps.cloud.asset_store.fetch_asset`
    or :func:`~apps.cloud.asset_store.fetch_presigned_asset`.
    """
    transfer_token = transfer_status.begin_transfer(
        stable_id, "download", bytes_total=bytes_total
    )
    try:
        if presigned_url is not None:
            result = fetch_presigned_asset(presigned_url, content_hash, dest)
        elif cfg is not None and s3 is not None:
            result = fetch_asset(cfg, s3, content_hash, dest)
        else:
            raise HydrationError(
                "fetch_asset_for_hydration needs cfg and s3 unless presigned_url is set"
            )
        if bytes_total is not None:
            transfer_status.update_transfer(stable_id, transfer_token, bytes_total)
        return result
    except AssetStoreError as exc:
        raise HydrationError(str(exc)) from exc
    finally:
        transfer_status.clear_transfer(stable_id, transfer_token)


def run_hydrate_job(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    s3: AssetS3Client,
    *,
    stable_id: str,
    asset_kind: str,
    machine_id: str,
    data_dir: Path,
    on_progress: Callable[[float, str], None] | None = None,
) -> None:
    """Download one remote asset into the local cache for ``stable_id``."""
    cache_dir = cloud_policy.artifact_cache_root(data_dir, asset_kind)
    source = resolve_playback_source(
        conn,
        stable_id,
        machine_id,
        asset_kind=asset_kind,
        cache_dir=cache_dir,
        cfg=cfg,
    )
    if source.origin in ("local", "cache"):
        raise HydrationError(
            f"{stable_id!r} is already playable from origin {source.origin!r}"
        )
    if source.origin == "unavailable":
        raise HydrationError(source.reason or f"{stable_id!r} is unavailable")
    if source.origin != "presigned":
        raise HydrationError(f"unexpected playback origin {source.origin!r}")
    if source.content_hash is None:
        raise HydrationError(
            f"no content_hash for {stable_id!r}; refusing hydration without "
            "a content-addressed key"
        )
    if source.url is None:
        raise HydrationError(f"presigned origin for {stable_id!r} has no URL")

    dest = cache_path(cache_dir, source.content_hash)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if on_progress is not None:
        on_progress(0.0, "downloading")
    if s3 is not None:
        fetch_asset_for_hydration(
            cfg,
            s3,
            source.content_hash,
            dest,
            stable_id=stable_id,
        )
    else:
        fetch_asset_for_hydration(
            cfg,
            None,
            source.content_hash,
            dest,
            stable_id=stable_id,
            presigned_url=source.url,
        )
    touch_cache_entry(dest)
    evict_cache(
        cloud_policy.artifact_cache_root(data_dir, asset_kind),
        cloud_policy.cache_budget_mb_for(asset_kind),
    )
    if on_progress is not None:
        on_progress(1.0, "hydrated")


def enqueue_hydrate_asset(
    store: JobStore | None,
    *,
    stable_id: str,
    asset_kind: str,
    machine_id: str,
    data_dir: Path,
) -> dict[str, Any]:
    """Enqueue a ``cloud.hydrate`` row; fail fast when jobs.db is not mounted."""
    if store is None:
        raise HydrationError(
            "jobs store is not available; cannot enqueue cloud.hydrate"
        )
    if not store.db_path.is_file():
        raise HydrationError(
            f"jobs database missing at {store.db_path}; refusing to enqueue "
            "cloud.hydrate without durable jobs.db"
        )
    from . import job as cloud_job

    return store.enqueue(
        cloud_job.JOB_KIND,
        payload={
            "stable_id": stable_id,
            "asset_kind": asset_kind,
            "machine_id": machine_id,
            "data_dir": str(data_dir),
        },
    )


# --- write path (push-then-delete) ------------------------------------------


@dataclass(frozen=True)
class ProduceOutcome:
    """What :func:`apply_policy_after_produce` did with a freshly made asset."""

    content_hash: str
    object_key: str
    uploaded: bool
    mode: PolicyMode
    policy_source: PolicySource
    local_action: LocalAction
    #: Where the bytes live locally now, or ``None`` when they were deleted.
    local_path: Path | None


def _now_iso() -> str:
    """This instant in the one format the sync protocol can order.

    ``datetime.now(UTC).isoformat()`` omits the microseconds entirely on the
    tick where they are zero, which is a second format for one column.
    """
    return sync_stamp.canonical_now()


def _upsert_remote_location(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    object_key: str,
    content_hash: str,
    machine_id: str,
    now: str,
) -> None:
    """Record the R2 copy in ``track_locations`` as ``kind='remote'``.

    ``remote_url`` stores the object KEY, not a URL: a presigned URL expires
    and differs on every mint, so persisting one would guarantee a stale row
    (ADR 06 point 4).

    Round 2 finding N1b: this used to write ``machine_id`` NULL and skip
    ``local_changelog`` entirely. The row was then invisible to every
    machine-scoped read (``locations.list_locations`` filters
    ``machine_id = owner``), so a track the daemon had just hydrated did not
    register as present on the machine that hydrated it, and it was only
    rescued by the next ``db.open_rw`` backfill -- which a long-lived daemon
    never performs. It also never reached the hub. Both fixed by going
    through :func:`apps.shared.state.sync_stamp.stamp_and_log` and naming
    ``machine_id`` explicitly, on the same natural key
    ``(stable_id, machine_id, kind, remote_url)`` the partial UNIQUE index
    asserts.
    """
    object_key = state_locations.normalize_stored_text(object_key)
    row = conn.execute(
        "SELECT location_id FROM track_locations "
        "WHERE stable_id = ? AND machine_id = ? AND kind = 'remote' "
        "AND remote_url = ?",
        (stable_id, machine_id, object_key),
    ).fetchone()
    location_id = str(row[0]) if row is not None else uuid.uuid4().hex
    stamp = sync_stamp.stamp_and_log(
        conn, _LOCATIONS_TABLE, (location_id,), machine_id, now=now,
    )
    if row is None:
        conn.execute(
            """
            INSERT INTO track_locations(
                location_id, stable_id, machine_id, kind, role, remote_url,
                available, probed_at, content_hash, created_at, updated_at,
                origin_device_id
            ) VALUES (?, ?, ?, 'remote', 'alternate', ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                location_id, stable_id, machine_id, object_key,
                stamp.updated_at, content_hash, stamp.updated_at,
                stamp.updated_at, stamp.origin_device_id,
            ),
        )
        return
    conn.execute(
        "UPDATE track_locations SET available = 1, probed_at = ?, "
        "content_hash = ?, updated_at = ?, origin_device_id = ?, "
        "deleted_at = NULL WHERE location_id = ?",
        (
            stamp.updated_at, content_hash, stamp.updated_at,
            stamp.origin_device_id, location_id,
        ),
    )


def _mark_local_unavailable(
    conn: sqlite3.Connection,
    stable_id: str,
    file_path: Path,
    machine_id: str,
    now: str,
) -> None:
    """Flag THIS machine's local copy as gone, stamped and logged.

    Scoped to ``machine_id`` (round 2 finding N1b, ADR 08 point 1): the
    unscoped UPDATE this replaces would mark another machine's row for the
    same path unavailable too, on the strength of a file deleted here. Each
    row is stamped individually so the edit carries an origin and reaches the
    push fence instead of moving ``updated_at`` with no changelog entry.
    """
    normalized = state_locations.normalize_stored_text(str(file_path))
    rows = conn.execute(
        "SELECT location_id FROM track_locations "
        "WHERE stable_id = ? AND machine_id = ? AND kind = 'local' "
        "AND file_path = ?",
        (stable_id, machine_id, normalized),
    ).fetchall()
    for (location_id,) in rows:
        stamp = sync_stamp.stamp_and_log(
            conn, _LOCATIONS_TABLE, (str(location_id),), machine_id, now=now,
        )
        conn.execute(
            "UPDATE track_locations SET available = 0, probed_at = ?, "
            "updated_at = ?, origin_device_id = ? WHERE location_id = ?",
            (
                stamp.updated_at, stamp.updated_at, stamp.origin_device_id,
                str(location_id),
            ),
        )


def apply_policy_after_produce(
    conn: sqlite3.Connection,
    s3: AssetS3Client,
    cfg: CloudConfig,
    *,
    stable_id: str,
    machine_id: str,
    local_path: Path,
    asset_kind: str,
    cache_dir: Path | None = None,
) -> ProduceOutcome:
    """Push a freshly produced asset to R2, then apply this machine's policy.

    The order is the whole safety property. The local copy is only ever
    touched AFTER :func:`apps.cloud.asset_store.object_exists` confirms the
    digest is durable in R2. A failed or rejected upload leaves the producer
    output exactly where it was and raises; there is no path through this
    function that deletes bytes it has not verified elsewhere.

    Per-mode local action:

    * ``pinned``  -- keep the file where the producer wrote it.
    * ``cached``  -- move it into ``cache_dir`` under its content-addressed
      name, so the next read is a cache hit rather than a download.
    * ``stream`` / ``excluded`` -- delete the local copy.
    """
    source = Path(local_path)
    if not source.is_file():
        raise HydrationError(f"produced asset missing at {source}")

    policy = resolve_policy(conn, stable_id, machine_id, asset_kind=asset_kind)
    transfer_token: str | None = None

    def _publish_upload_progress(transferred: int, total: int) -> None:
        nonlocal transfer_token
        # push_asset invokes this only after its object-exists check, at the
        # point a real request body is about to be consumed. An already
        # durable object therefore never flashes a fictitious transfer.
        if transfer_token is None:
            transfer_token = transfer_status.begin_transfer(
                stable_id, "upload", bytes_total=total
            )
        transfer_status.update_transfer(stable_id, transfer_token, transferred)

    try:
        result = push_asset(
            cfg,
            s3,
            source,
            on_progress=_publish_upload_progress,
        )
    finally:
        # The ledger is intentionally a live-operation view. Do not leave a
        # completed or failed upload looking like a still-running transfer.
        if transfer_token is not None:
            transfer_status.clear_transfer(stable_id, transfer_token)
    if not object_exists(cfg, s3, result.content_hash):
        raise HydrationError(
            f"push of {source} reported success but {result.object_key} is "
            "not readable back from R2; local copy left untouched"
        )

    now = _now_iso()
    # One unit: the row write and its local_changelog entry commit together
    # or not at all (ADR 08 point 3). The filesystem moves stay outside it --
    # they are not transactional, and the push-then-delete ORDER above is what
    # makes them safe, not the transaction.
    with sync_stamp.stamped_transaction(conn):
        _upsert_remote_location(
            conn,
            stable_id=stable_id,
            object_key=result.object_key,
            content_hash=result.content_hash,
            machine_id=machine_id,
            now=now,
        )

    if policy.mode == "pinned":
        action: LocalAction = "kept"
        final_path: Path | None = source
    elif policy.mode == "cached":
        if cache_dir is None:
            raise HydrationError(
                f"policy 'cached' for {stable_id!r} needs a cache_dir to move "
                f"{source} into; refusing to delete or to leave it unmanaged."
            )
        final_path = cache_path(cache_dir, result.content_hash)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        source.replace(final_path)
        with sync_stamp.stamped_transaction(conn):
            _mark_local_unavailable(conn, stable_id, source, machine_id, now)
        action = "cached"
        evict_cache(cache_dir, cloud_policy.cache_budget_mb_for(asset_kind))
    elif policy.mode in ("stream", "excluded"):
        source.unlink()
        with sync_stamp.stamped_transaction(conn):
            _mark_local_unavailable(conn, stable_id, source, machine_id, now)
        action = "deleted"
        final_path = None
    else:
        raise HydrationError(f"unhandled policy mode {policy.mode!r}")

    return ProduceOutcome(
        content_hash=result.content_hash,
        object_key=result.object_key,
        uploaded=result.uploaded,
        mode=policy.mode,
        policy_source=policy.source,
        local_action=action,
        local_path=final_path,
    )


__all__ = [
    "ASSET_KINDS",
    "BYTES_PER_MB",
    "MODE_STRENGTH",
    "EvictionResult",
    "HydrationError",
    "PlaybackSource",
    "PolicyResolution",
    "ProduceOutcome",
    "apply_policy_after_produce",
    "cache_path",
    "enqueue_hydrate_asset",
    "evict_cache",
    "fetch_asset_for_hydration",
    "run_hydrate_job",
    "resolve_playback_source",
    "resolve_policy",
    "touch_cache_entry",
]
