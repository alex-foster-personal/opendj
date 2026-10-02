"""Cache layout, policy resolution and the READ PATH of ADR 06.

Split out of :mod:`apps.cloud.hydration` (quality-gate file_size ratchet,
round 4): everything here is read-only over the v6 policy tables
(``specs/design_decision_05.md``); the WRITE PATH (push-then-delete) stayed
in ``hydration.py``, which imports :func:`resolve_policy` and
:func:`cache_path` back from here -- a one-way dependency, never the other
way round.

Believed-state unions four inputs on every read:

1. durable ``track_locations`` (including ``available`` probe stamps);
2. ephemeral in-process ``transfer_status`` (progress only, never presence);
3. on-disk cache bytes under the kind's ``local_cache_path`` root; and
4. ``apps.cloud.policy.CFG`` cache budgets (used by eviction after writes).

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

:func:`resolve_policy` invents no default: a machine with no
``sync_policies`` row for the asset kind raises
:class:`~apps.cloud.eviction.HydrationError` rather than silently reading
as ``stream``. :func:`resolve_playback_source` is the one exception: when
there is no policy row but a pinned local file still exists on disk, local
bytes are returned (implicit local-only installs never configure CloudSync).
Missing row with no on-disk local copy still refuses loudly; a configured
``excluded`` policy still wins over local bytes.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .asset_store import DEFAULT_PRESIGN_EXPIRY_SECONDS, presign_url, validate_content_hash
from .config import CloudConfig
from .eviction import HydrationError
from .r2_keys import canonical_digest

PolicyMode = Literal["pinned", "cached", "stream", "excluded"]
PolicySource = Literal["sync_policies", "playlist_pin", "unconfigured"]
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

#: ``sync_policies.asset_kind`` CHECK vocabulary (schema v10). ``lyrics_cache``
#: and ``karaoke_words`` were admitted when _V10 rebuilt the CHECK; this tuple
#: and that CHECK are pinned equal by tests/cloud/test_asset_kind_vocabulary.py.
ASSET_KINDS: tuple[str, ...] = (
    "audio",
    "stem_bundle",
    "anlz_cache",
    "vocal_cache",
    "lyrics_cache",
    "karaoke_words",
)


# --- cache layout ----------------------------------------------------------


def cache_path(cache_dir: Path, content_hash: str) -> Path:
    """Local cache location for a digest.

    Mirrors the R2 key layout minus the ``assets/`` prefix, so an operator
    reading a cache directory and an R2 listing sees the same shard shape.
    """
    digest = validate_content_hash(content_hash)
    return Path(cache_dir) / digest[:2] / digest


def touch_cache_entry(path: Path) -> None:
    """Bump the access time of a cache hit, preserving mtime.

    :func:`apps.cloud.eviction.evict_cache` is LRU by ``atime``. Many
    volumes are mounted ``relatime`` or ``noatime``, where a plain read does
    NOT move atime, so the read path stamps it explicitly. Without this the
    LRU degrades into "evict whatever was written first", which is a
    different policy wearing the same name.
    """
    target = Path(path)
    stat_result = target.stat()
    now = datetime.now(UTC).timestamp()
    os.utime(target, (now, stat_result.st_mtime))


# --- policy resolution -------------------------------------------------------


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


class PolicyUnconfigured(HydrationError):
    """No ``sync_policies`` row for this machine and asset kind.

    Raised by :func:`resolve_policy`, which refuses to assume a mode.
    :func:`resolve_playback_source` treats an unconfigured machine as
    local-only (CLOUDSYNC-33): it plays its own copies and reports anything
    else as not on this computer, never as a policy error a DJ cannot act on.
    """


#: What a deck shows for a track this machine has no copy of and no CloudSync
#: policy to fetch one by (CLOUDSYNC-33).
NOT_ON_THIS_MACHINE = "This file isn't on this computer."


def _default_mode(
    conn: sqlite3.Connection, machine_id: str, asset_kind: str
) -> PolicyMode:
    row = conn.execute(
        "SELECT mode FROM sync_policies "
        "WHERE machine_id = ? AND asset_kind = ? AND deleted_at IS NULL",
        (machine_id, asset_kind),
    ).fetchone()
    if row is None:
        raise PolicyUnconfigured(
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


# --- read path ---------------------------------------------------------------


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


def _unavailable_marked_local(
    conn: sqlite3.Connection, stable_id: str, machine_id: str
) -> PlaybackSource | None:
    """Return unavailable when a local row is explicitly marked gone.

    ``available=0`` is a deliberate probe stamp: the operator or eviction
    path declared the path not playable on this machine. Do not fall through
    to presigned R2 for that case.
    """
    rows = conn.execute(
        """
        SELECT location_id, file_path
        FROM track_locations
        WHERE stable_id = ?
          AND machine_id = ?
          AND kind = 'local'
          AND available = 0
          AND deleted_at IS NULL
          AND file_path IS NOT NULL
          AND file_path != ''
        ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END, rowid
        """,
        (stable_id, machine_id),
    ).fetchall()
    for location_id, file_path in rows:
        candidate = Path(str(file_path))
        if candidate.is_file():
            continue
        return PlaybackSource(
            origin="unavailable",
            mode="pinned",
            policy_source="sync_policies",
            reason=(
                f"track_locations row {location_id!r} marked unavailable "
                f"(available=0) at {file_path!r}"
            ),
        )
    return None


def _local_file(
    conn: sqlite3.Connection, stable_id: str, machine_id: str
) -> Path | None:
    """First materialised on-disk local copy for ``machine_id``, else ``None``.

    Delegates to :func:`apps.shared.state.locations.local_audio_path`, which
    prefers ``track_locations`` then falls back to ``tracks.file_path`` so
    local-only imports without CloudSync rows still resolve.
    """
    from apps.shared import fs_residency
    from apps.shared.state import locations as state_locations

    # FIFOs and other special nodes are playable paths whose open() the audio
    # route probes under a timeout (#766, #2749); the strict materialised gate
    # would read them as absent and fall through to the cloud policy.
    return state_locations.local_audio_path(
        conn,
        stable_id,
        machine_id=machine_id,
        residency=fs_residency.exists_for_audio_open_probe,
    )


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
    local = _local_file(conn, stable_id, machine_id)
    try:
        policy = resolve_policy(conn, stable_id, machine_id, asset_kind=asset_kind)
    except HydrationError as exc:
        # An unconfigured machine is local-only (CLOUDSYNC-33): its own copy
        # plays, and a track it has no copy of is not on this computer. It
        # never streams or hydrates, because nothing chose a mode that would.
        # Any other policy error still lets a local copy play, but refuses
        # loudly when there is none.
        content_hash = _content_hash(conn, stable_id)
        if local is not None:
            return PlaybackSource(
                origin="local",
                mode="pinned",
                policy_source="sync_policies",
                path=local,
                content_hash=content_hash,
            )
        if not isinstance(exc, PolicyUnconfigured):
            raise
        return PlaybackSource(
            origin="unavailable",
            mode="excluded",
            policy_source="unconfigured",
            content_hash=content_hash,
            reason=NOT_ON_THIS_MACHINE,
        )

    content_hash = _content_hash(conn, stable_id)

    if policy.mode == "excluded":
        return PlaybackSource(
            origin="unavailable",
            mode=policy.mode,
            policy_source=policy.source,
            content_hash=content_hash,
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

    if local is not None:
        return PlaybackSource(
            origin="local",
            mode=policy.mode,
            policy_source=policy.source,
            path=local,
            content_hash=content_hash,
        )

    marked_unavailable = _unavailable_marked_local(conn, stable_id, machine_id)
    if marked_unavailable is not None:
        return PlaybackSource(
            origin="unavailable",
            mode=policy.mode,
            policy_source=policy.source,
            content_hash=content_hash,
            reason=marked_unavailable.reason,
        )

    unhydrated_pin = policy.mode == "pinned"

    if content_hash is None:
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

    # The state layer stores ``sha256:<hex>`` (apps.shared.hashing); a key
    # and a cache entry can only be derived from the bare hex. Validate only
    # once a remote lookup is required: local and excluded paths need no R2 key.
    digest = canonical_digest(content_hash)
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


__all__ = [
    "ASSET_KINDS",
    "MODE_STRENGTH",
    "PlaybackSource",
    "PolicyResolution",
    "cache_path",
    "resolve_playback_source",
    "resolve_policy",
    "touch_cache_entry",
]
