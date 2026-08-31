"""Policy-driven playback resolution, cache eviction, push-then-delete.

Implements the READ PATH and WRITE PATH of ADR 06
(``specs/design_decision_06.md``) over the v6 policy tables
(``specs/design_decision_05.md``).

READ PATH (:func:`resolve_playback_source`), in ADR order:

1. a pinned local file -- ``track_locations`` ``kind='local'``,
   ``available=1``, and the path still on disk;
2. else a hot-cache hit keyed by ``content_hash`` (never by URL: a presigned
   signature differs on every mint, so a URL-keyed cache never hits);
3. else a freshly minted presigned R2 URL.

WHICH policy applies is a separate question from WHICH source wins. The
policy (``pinned`` / ``cached`` / ``stream`` / ``excluded``) governs what a
machine KEEPS; the resolution order above governs what it READS. A local
copy that happens to exist is always the cheapest read even under
``stream``. The one policy that short-circuits is ``excluded``: the machine
is declared not to carry the asset kind at all, so no URL is minted for it.

Policy comes from ``sync_policies(machine_id, asset_kind)``, and a
``playlist_pins`` row for any playlist containing the track OVERRIDES that
default. A track sitting in two pinned playlists with conflicting modes
resolves to the strongest (see :data:`MODE_STRENGTH`) -- a pin exists to
guarantee availability, so the strongest claim wins, and the tiebreak is
``playlist_id`` order so the answer never depends on row order.

Nothing here invents a default. A machine with no ``sync_policies`` row for
the asset kind raises :class:`HydrationError` rather than silently reading
as ``stream``; that is the difference between an unconfigured fleet you can
see and one that quietly streams a gig over venue wifi.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .asset_store import (
    DEFAULT_PRESIGN_EXPIRY_SECONDS,
    AssetS3Client,
    object_exists,
    presign_url,
    push_asset,
    validate_content_hash,
)
from .config import CloudConfig
from .eviction import BYTES_PER_MB, EvictionResult, HydrationError, evict_cache

PolicyMode = Literal["pinned", "cached", "stream", "excluded"]
PolicySource = Literal["sync_policies", "playlist_pin"]
Origin = Literal["local", "cache", "presigned", "unavailable"]
LocalAction = Literal["kept", "cached", "deleted"]

#: Ordered strongest-first. Used to resolve a track pinned by two playlists
#: with different modes, and nothing else.
MODE_STRENGTH: dict[str, int] = {
    "pinned": 3,
    "cached": 2,
    "stream": 1,
    "excluded": 0,
}

#: ``sync_policies.asset_kind`` CHECK vocabulary (schema v6).
ASSET_KINDS: tuple[str, ...] = (
    "audio",
    "stem_bundle",
    "anlz_cache",
    "vocal_cache",
)


# --- cache layout --------------------------------------------------------


def cache_path(cache_dir: Path, content_hash: str) -> Path:
    """Local cache location for a digest.

    Mirrors the R2 key layout minus the ``assets/`` prefix, so an operator
    reading a cache directory and an R2 listing sees the same shard shape.
    """
    digest = validate_content_hash(content_hash)
    return Path(cache_dir) / digest[:2] / digest


def touch_cache_entry(path: Path) -> None:
    """Bump the access time of a cache hit, preserving mtime.

    :func:`evict_cache` is LRU by ``atime``. Many volumes are mounted
    ``relatime`` or ``noatime``, where a plain read does NOT move atime, so
    the read path stamps it explicitly. Without this the LRU degrades into
    "evict whatever was written first", which is a different policy wearing
    the same name.
    """
    target = Path(path)
    stat_result = target.stat()
    now = datetime.now(UTC).timestamp()
    os.utime(target, (now, stat_result.st_mtime))


# --- policy resolution ---------------------------------------------------


@dataclass(frozen=True)
class PolicyResolution:
    """Which mode applies to one track on one machine, and why."""

    mode: PolicyMode
    source: PolicySource
    asset_kind: str
    #: Set only when ``source == 'playlist_pin'``.
    playlist_id: str | None = None


def _validate_asset_kind(asset_kind: str) -> str:
    if asset_kind not in ASSET_KINDS:
        raise HydrationError(
            f"asset_kind {asset_kind!r} is not one of {list(ASSET_KINDS)}; "
            "sync_policies has a CHECK constraint on exactly these."
        )
    return asset_kind


def _default_mode(
    conn: sqlite3.Connection, machine_id: str, asset_kind: str
) -> PolicyMode:
    row = conn.execute(
        "SELECT mode FROM sync_policies "
        "WHERE machine_id = ? AND asset_kind = ? AND deleted_at IS NULL",
        (machine_id, asset_kind),
    ).fetchone()
    if row is None:
        raise HydrationError(
            f"no sync_policies row for machine {machine_id!r} / asset_kind "
            f"{asset_kind!r}. Configure the machine in the CloudSync panel "
            "(or POST /api/v1/cloudsync/policies) before resolving playback; "
            "refusing to assume a mode."
        )
    return _validate_mode(str(row[0]))


def _validate_mode(mode: str) -> PolicyMode:
    if mode not in MODE_STRENGTH:
        raise HydrationError(
            f"mode {mode!r} is not one of {sorted(MODE_STRENGTH)}"
        )
    return mode  # type: ignore[return-value]


def _pin_override(
    conn: sqlite3.Connection, stable_id: str, machine_id: str
) -> tuple[str, PolicyMode] | None:
    """Strongest playlist pin covering ``stable_id`` on ``machine_id``."""
    rows = conn.execute(
        """
        SELECT pp.playlist_id, pp.mode
        FROM playlist_pins pp
        JOIN playlist_memberships pm ON pm.playlist_id = pp.playlist_id
        WHERE pp.machine_id = ?
          AND pm.stable_id = ?
          AND pp.deleted_at IS NULL
          AND pm.deleted_at IS NULL
        """,
        (machine_id, stable_id),
    ).fetchall()
    if not rows:
        return None
    ranked = sorted(
        ((str(pid), _validate_mode(str(mode))) for pid, mode in rows),
        key=lambda item: (-MODE_STRENGTH[item[1]], item[0]),
    )
    return ranked[0]


def resolve_policy(
    conn: sqlite3.Connection,
    stable_id: str,
    machine_id: str,
    *,
    asset_kind: str,
) -> PolicyResolution:
    """Return the mode in force for one track on one machine.

    A playlist pin beats the per-asset-kind default unconditionally, in both
    directions: a pin can promote a streamed track to ``pinned`` for a gig
    crate, and can exclude a track the machine would otherwise cache.
    """
    kind = _validate_asset_kind(asset_kind)
    default = _default_mode(conn, machine_id, kind)
    pin = _pin_override(conn, stable_id, machine_id)
    if pin is None:
        return PolicyResolution(mode=default, source="sync_policies", asset_kind=kind)
    playlist_id, pinned_mode = pin
    return PolicyResolution(
        mode=pinned_mode,
        source="playlist_pin",
        asset_kind=kind,
        playlist_id=playlist_id,
    )


# --- read path -----------------------------------------------------------


@dataclass(frozen=True)
class PlaybackSource:
    """Where this machine should read the track from, right now."""

    origin: Origin
    mode: PolicyMode
    policy_source: PolicySource
    path: Path | None = None
    url: str | None = None
    content_hash: str | None = None
    expires_in_seconds: int | None = None
    #: True when policy says ``pinned`` but no local copy is on disk. ADR 06
    #: consequence 4: nothing warns automatically in v1, so the flag is what
    #: the config-UI table renders.
    pin_unhydrated: bool = False
    #: Set only when ``origin == 'unavailable'``; always names the reason.
    reason: str | None = None


def _local_file(conn: sqlite3.Connection, stable_id: str) -> Path | None:
    """First on-disk local copy, primary role first, else ``None``.

    ``available`` is a probe stamp, not a live fact: it stays 1 after an
    eviction or an unmounted volume. Trusting it alone would hand the player
    a path that 404s, so the row is re-checked against the filesystem.
    """
    rows = conn.execute(
        """
        SELECT file_path
        FROM track_locations
        WHERE stable_id = ?
          AND kind = 'local'
          AND available = 1
          AND deleted_at IS NULL
          AND file_path IS NOT NULL
          AND file_path != ''
        ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END, rowid
        """,
        (stable_id,),
    ).fetchall()
    for (file_path,) in rows:
        candidate = Path(str(file_path))
        if candidate.is_file():
            return candidate
    return None


def _content_hash(conn: sqlite3.Connection, stable_id: str) -> str | None:
    """Digest for the track, preferring a remote row's hash.

    A remote row's hash is the one an R2 key was actually minted from; the
    local and ``tracks`` copies are the ingest/backfill view of the same
    file and are used only as fallbacks.
    """
    row = conn.execute(
        """
        SELECT content_hash
        FROM track_locations
        WHERE stable_id = ?
          AND deleted_at IS NULL
          AND content_hash IS NOT NULL
          AND content_hash != ''
        ORDER BY CASE kind WHEN 'remote' THEN 0 ELSE 1 END, rowid
        """,
        (stable_id,),
    ).fetchone()
    if row is not None:
        return str(row[0])
    row = conn.execute(
        "SELECT content_hash FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    if row is None:
        raise HydrationError(f"no tracks row for stable_id {stable_id!r}")
    return str(row[0]) if row[0] else None


def resolve_playback_source(
    conn: sqlite3.Connection,
    stable_id: str,
    machine_id: str,
    *,
    asset_kind: str,
    cache_dir: Path,
    cfg: CloudConfig | None = None,
    expiry_seconds: int = DEFAULT_PRESIGN_EXPIRY_SECONDS,
) -> PlaybackSource:
    """Resolve the single source this machine should play ``stable_id`` from.

    ``cfg`` may be omitted while a machine only ever serves pinned or cached
    audio (ADR 06 consequence 2). If resolution actually reaches the presign
    step without one, that is a misconfiguration and raises rather than
    returning a source the player cannot use.
    """
    policy = resolve_policy(conn, stable_id, machine_id, asset_kind=asset_kind)
    digest = _content_hash(conn, stable_id)

    if policy.mode == "excluded":
        return PlaybackSource(
            origin="unavailable",
            mode=policy.mode,
            policy_source=policy.source,
            content_hash=digest,
            reason=(
                f"policy 'excluded' for asset_kind {policy.asset_kind!r} on "
                f"machine {machine_id!r}"
                + (
                    f" (via playlist pin {policy.playlist_id})"
                    if policy.playlist_id
                    else ""
                )
            ),
        )

    local = _local_file(conn, stable_id)
    if local is not None:
        return PlaybackSource(
            origin="local",
            mode=policy.mode,
            policy_source=policy.source,
            path=local,
            content_hash=digest,
        )

    unhydrated_pin = policy.mode == "pinned"

    if digest is None:
        return PlaybackSource(
            origin="unavailable",
            mode=policy.mode,
            policy_source=policy.source,
            pin_unhydrated=unhydrated_pin,
            reason=(
                f"no local copy and no content_hash for {stable_id!r}; run the "
                "content_hash backfill before this track can stream"
            ),
        )

    cached = cache_path(cache_dir, digest)
    if cached.is_file():
        touch_cache_entry(cached)
        return PlaybackSource(
            origin="cache",
            mode=policy.mode,
            policy_source=policy.source,
            path=cached,
            content_hash=digest,
            pin_unhydrated=unhydrated_pin,
        )

    if cfg is None:
        raise HydrationError(
            f"{stable_id!r} resolves to a presigned R2 read but no CloudConfig "
            "was supplied. Pass one loaded from Doppler "
            "(R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)."
        )
    return PlaybackSource(
        origin="presigned",
        mode=policy.mode,
        policy_source=policy.source,
        url=presign_url(cfg, digest, expiry_seconds),
        content_hash=digest,
        expires_in_seconds=expiry_seconds,
        pin_unhydrated=unhydrated_pin,
    )


# --- cache eviction --------------------------------------------------------
#
# EvictionResult / evict_cache live in apps/cloud/eviction.py (round 2
# hardening file-size split) and are imported above; re-exported below so
# `hydration.evict_cache` / `hydration.EvictionResult` keep working for
# existing callers.


# --- write path (push-then-delete) ---------------------------------------


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
    return datetime.now(UTC).isoformat()


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
    """
    row = conn.execute(
        "SELECT rowid FROM track_locations "
        "WHERE stable_id = ? AND kind = 'remote' AND remote_url = ?",
        (stable_id, object_key),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO track_locations(
                stable_id, kind, role, remote_url, available, probed_at,
                content_hash, created_at, updated_at, origin_device_id
            ) VALUES (?, 'remote', 'alternate', ?, 1, ?, ?, ?, ?, ?)
            """,
            (stable_id, object_key, now, content_hash, now, now, machine_id),
        )
        return
    conn.execute(
        "UPDATE track_locations SET available = 1, probed_at = ?, "
        "content_hash = ?, updated_at = ?, origin_device_id = ?, "
        "deleted_at = NULL WHERE rowid = ?",
        (now, content_hash, now, machine_id, int(row[0])),
    )


def _mark_local_unavailable(
    conn: sqlite3.Connection, stable_id: str, file_path: Path, now: str
) -> None:
    conn.execute(
        "UPDATE track_locations SET available = 0, probed_at = ?, "
        "updated_at = ? WHERE stable_id = ? AND kind = 'local' AND file_path = ?",
        (now, now, stable_id, str(file_path)),
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
    result = push_asset(cfg, s3, source)
    if not object_exists(cfg, s3, result.content_hash):
        raise HydrationError(
            f"push of {source} reported success but {result.object_key} is "
            "not readable back from R2; local copy left untouched"
        )

    now = _now_iso()
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
        _mark_local_unavailable(conn, stable_id, source, now)
        action = "cached"
    elif policy.mode in ("stream", "excluded"):
        source.unlink()
        _mark_local_unavailable(conn, stable_id, source, now)
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
    "evict_cache",
    "resolve_playback_source",
    "resolve_policy",
    "touch_cache_entry",
]
