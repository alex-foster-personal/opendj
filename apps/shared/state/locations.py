"""Multiple playable locations per track, and the backend picker.

``tracks.file_path`` stays the ingest/legacy primary path. This table holds
extra copies: another machine, a remote URL, a lower-bitrate transcode.
The play path picks one winner. The frontend is never given the list.

Pick order (fail-closed, no invented fallbacks that hide a miss):

1. Drop broken (not materialised after path-map).
2. Prefer ``local`` over ``remote`` when any local copy works.
3. Apply the configured venue window (min/max rank). If that window is
   empty, use the working pool rather than 404 a playable file.
4. Highest venue rank wins; ties keep insertion order.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Sequence

from apps.shared import audio_quality, fs_residency, platform_paths

Kind = Literal["local", "remote"]
Role = Literal["primary", "alternate"]

AUDIO_MEDIA_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}

MIN_VENUE_ENV = "MDT_AUDIO_MIN_VENUE"
MAX_VENUE_ENV = "MDT_AUDIO_MAX_VENUE"
SHARE_MAX_VENUE_ENV = "MDT_AUDIO_SHARE_MAX_VENUE"


class LocationError(ValueError):
    """Invalid location row or pick policy."""


@dataclass(frozen=True)
class TrackLocation:
    id: int
    stable_id: str
    kind: Kind
    role: Role
    file_path: Optional[str]
    remote_url: Optional[str]
    venue_key: Optional[str]
    venue_rank: Optional[int]
    available: bool
    probed_at: Optional[str]
    content_hash: Optional[str]


@dataclass(frozen=True)
class PickPolicy:
    """Venue window the picker may choose inside.

    Ranks come from ``apps.shared.audio_quality.VENUES`` (0 naughty_step
    through 5 stadium). ``prefer_local`` is the local>remote rule.
    """

    prefer_local: bool = True
    min_venue_rank: int = 0
    max_venue_rank: int = 5


@dataclass(frozen=True)
class PickedAudio:
    path: Path
    media_type: str
    kind: Kind
    venue_key: Optional[str]
    venue_rank: Optional[int]
    source: str


@dataclass(frozen=True)
class _Candidate:
    kind: Kind
    path: Path
    media_type: str
    venue_key: Optional[str]
    venue_rank: Optional[int]
    source: str
    working: bool


def policy_from_env(*, share: bool = False) -> PickPolicy:
    """Build a pick policy from env. Unknown venue keys fail loud."""
    min_key = os.environ.get(MIN_VENUE_ENV, "naughty_step")
    default_max = "warehouse" if share else "stadium"
    max_key = os.environ.get(
        SHARE_MAX_VENUE_ENV if share else MAX_VENUE_ENV,
        default_max,
    )
    return PickPolicy(
        prefer_local=True,
        min_venue_rank=_venue_rank(min_key),
        max_venue_rank=_venue_rank(max_key),
    )


def _venue_rank(key: str) -> int:
    venue = audio_quality.BY_KEY.get(key)
    if venue is None:
        known = ", ".join(v.key for v in audio_quality.VENUES)
        raise LocationError(f"unknown venue {key!r}; expected one of: {known}")
    return venue.rank


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def locations_table_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_locations'"
    ).fetchone()
    return row is not None


def list_locations(conn: sqlite3.Connection, stable_id: str) -> list[TrackLocation]:
    if not locations_table_ready(conn):
        return []
    rows = conn.execute(
        "SELECT id, stable_id, kind, role, file_path, remote_url, "
        "venue_key, venue_rank, available, probed_at, content_hash "
        "FROM track_locations WHERE stable_id = ? ORDER BY id",
        (stable_id,),
    ).fetchall()
    return [_row_to_location(row) for row in rows]


def list_location_paths(
    conn: sqlite3.Connection, stable_ids: Sequence[str]
) -> dict[str, list[str]]:
    """file_path values per stable_id, for bulk availability."""
    out: dict[str, list[str]] = {sid: [] for sid in stable_ids}
    if not stable_ids or not locations_table_ready(conn):
        return out
    placeholders = ",".join("?" * len(stable_ids))
    rows = conn.execute(
        f"SELECT stable_id, file_path FROM track_locations "
        f"WHERE stable_id IN ({placeholders}) AND file_path IS NOT NULL",
        tuple(stable_ids),
    ).fetchall()
    for stable_id, file_path in rows:
        if file_path:
            out.setdefault(stable_id, []).append(str(file_path))
    return out


def upsert_location(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    kind: Kind,
    file_path: Optional[str] = None,
    remote_url: Optional[str] = None,
    role: Role = "alternate",
    content_hash: Optional[str] = None,
    now: Optional[str] = None,
) -> int:
    """Insert or refresh one location. Returns the row id."""
    if kind not in ("local", "remote"):
        raise LocationError(f"kind must be local or remote, got {kind!r}")
    if role not in ("primary", "alternate"):
        raise LocationError(f"role must be primary or alternate, got {role!r}")
    if not file_path and not remote_url:
        raise LocationError("location needs file_path or remote_url")
    ts = now or _now_iso()
    venue_key, venue_rank, available = _probe_file(file_path)
    existing = _find_existing(conn, stable_id, kind, file_path, remote_url)
    if existing is None:
        cur = conn.execute(
            "INSERT INTO track_locations("
            "stable_id, kind, role, file_path, remote_url, venue_key, "
            "venue_rank, available, probed_at, content_hash, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stable_id,
                kind,
                role,
                file_path,
                remote_url,
                venue_key,
                venue_rank,
                1 if available else 0,
                ts,
                content_hash,
                ts,
                ts,
            ),
        )
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE track_locations SET role=?, venue_key=?, venue_rank=?, "
        "available=?, probed_at=?, content_hash=?, updated_at=? WHERE id=?",
        (
            role,
            venue_key,
            venue_rank,
            1 if available else 0,
            ts,
            content_hash,
            ts,
            existing,
        ),
    )
    return existing


def sync_primary_local(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    file_path: Optional[str],
    now: Optional[str] = None,
) -> None:
    """Keep the ingest primary row aligned with ``tracks.file_path``."""
    if not file_path or not locations_table_ready(conn):
        return
    upsert_location(
        conn,
        stable_id=stable_id,
        kind="local",
        file_path=file_path,
        role="primary",
        now=now,
    )


def pick_playable(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    policy: Optional[PickPolicy] = None,
    folder_path: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> Optional[PickedAudio]:
    """Return the single playable winner, or None when nothing works."""
    policy = policy or policy_from_env()
    duration = duration_ms
    if duration is None:
        row = conn.execute(
            "SELECT duration_ms, file_path FROM tracks WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        if row is None:
            return None
        duration = row[0]
        implicit_file = row[1]
    else:
        implicit_file = conn.execute(
            "SELECT file_path FROM tracks WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        implicit_file = implicit_file[0] if implicit_file else None

    seen: set[str] = set()
    candidates: list[_Candidate] = []
    for loc in list_locations(conn, stable_id):
        cand = _candidate_from_path(
            loc.file_path, kind=loc.kind, source=f"location:{loc.id}",
            duration_ms=duration,
        )
        if cand is not None:
            seen.add(str(cand.path))
            candidates.append(cand)
    for raw, source, kind in (
        (implicit_file, "file_path", "local"),
        (folder_path, "folder_path", "local"),
    ):
        cand = _candidate_from_path(
            raw, kind=kind, source=source, duration_ms=duration,
        )
        if cand is not None and str(cand.path) not in seen:
            seen.add(str(cand.path))
            candidates.append(cand)

    working = [c for c in candidates if c.working]
    if not working:
        return None
    pool = working
    if policy.prefer_local:
        locals_only = [c for c in working if c.kind == "local"]
        if locals_only:
            pool = locals_only
    windowed = [
        c for c in pool
        if c.venue_rank is not None
        and policy.min_venue_rank <= c.venue_rank <= policy.max_venue_rank
    ]
    chosen_pool = windowed or pool
    winner = max(
        chosen_pool,
        key=lambda c: (c.venue_rank if c.venue_rank is not None else -1),
    )
    return PickedAudio(
        path=winner.path,
        media_type=winner.media_type,
        kind=winner.kind,
        venue_key=winner.venue_key,
        venue_rank=winner.venue_rank,
        source=winner.source,
    )


def _find_existing(
    conn: sqlite3.Connection,
    stable_id: str,
    kind: str,
    file_path: Optional[str],
    remote_url: Optional[str],
) -> Optional[int]:
    if file_path:
        row = conn.execute(
            "SELECT id FROM track_locations "
            "WHERE stable_id=? AND kind=? AND file_path=?",
            (stable_id, kind, file_path),
        ).fetchone()
        return int(row[0]) if row else None
    row = conn.execute(
        "SELECT id FROM track_locations "
        "WHERE stable_id=? AND kind=? AND remote_url=?",
        (stable_id, kind, remote_url),
    ).fetchone()
    return int(row[0]) if row else None


def _probe_file(file_path: Optional[str]) -> tuple[Optional[str], Optional[int], bool]:
    if not file_path:
        return None, None, False
    mapped = platform_paths.resolve_asset_path(file_path)
    if mapped.resolved is None or not fs_residency.is_materialised(mapped.resolved):
        return None, None, False
    quality = audio_quality.classify(str(mapped.resolved), None)
    if quality.venue is None:
        return None, None, True
    return quality.venue.key, quality.venue.rank, True


def _candidate_from_path(
    raw: Optional[str],
    *,
    kind: Kind,
    source: str,
    duration_ms: Optional[int],
) -> Optional[_Candidate]:
    if not raw or raw.startswith(platform_paths.STREAMING_PREFIXES):
        return None
    mapped = platform_paths.resolve_asset_path(raw)
    if mapped.resolved is None:
        return None
    path = mapped.resolved
    media_type = AUDIO_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        return None
    working = fs_residency.is_materialised(path)
    size = fs_residency.materialised_size(path) if working else None
    quality = audio_quality.classify(str(path), duration_ms, size)
    return _Candidate(
        kind=kind,
        path=path,
        media_type=media_type,
        venue_key=quality.venue.key if quality.venue else None,
        venue_rank=quality.venue.rank if quality.venue else None,
        source=source,
        working=working,
    )


def _row_to_location(row: tuple[object, ...]) -> TrackLocation:
    return TrackLocation(
        id=int(row[0]),
        stable_id=str(row[1]),
        kind=row[2],  # type: ignore[arg-type]
        role=row[3],  # type: ignore[arg-type]
        file_path=str(row[4]) if row[4] else None,
        remote_url=str(row[5]) if row[5] else None,
        venue_key=str(row[6]) if row[6] else None,
        venue_rank=int(row[7]) if row[7] is not None else None,
        available=bool(row[8]),
        probed_at=str(row[9]) if row[9] else None,
        content_hash=str(row[10]) if row[10] else None,
    )
