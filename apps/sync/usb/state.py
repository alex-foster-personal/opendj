"""Canonical-tracks loader for the USB sync diff engine.

Two modes
---------

* **Shared-state** (future): reads from ``apps.shared.state`` once Phase 5
  publishes the ``tracks`` + ``playlists`` + ``memberships`` tables.
* **Rekordbox-direct shim** (now): reads playlist contents directly from
  a Rekordbox ``master.db`` via :mod:`pyrekordbox` and computes
  ``stable_id`` + ``content_hash`` locally.

The shim stays behind the same :class:`CanonicalTrack` contract so the
diff engine is blind to which backend sourced the data.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from apps.shared.hashing import HashCache, sha256_file
from apps.shared.stable_id import stable_id_str


@dataclass(slots=True, frozen=True)
class CanonicalTrack:
    """A track selected by a profile, ready for diff/copy."""

    stable_id: str
    playlist: str              # which profile-selected playlist this came from
    source_path: Path          # absolute path on local disk
    title: str
    artist: str
    album: str
    duration_ms: int | None
    size_bytes: int
    isrc: str | None
    content_hash: str          # "sha256:<hex>"


def _compute_content_hash(
    path: Path,
    cache: HashCache | None,
) -> str:
    if cache is not None:
        return cache.hash_with_cache(path)
    return sha256_file(path)


def _coerce_duration(value) -> int | None:
    """Rekordbox stores length in centiseconds (duration / 10 ms)."""
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    # Heuristic: pyrekordbox's Length field is seconds; others might be ms.
    # We keep it simple and return ms assuming seconds input.
    return v * 1000 if v < 100_000 else v


def _isrc_from_rb(track) -> str | None:
    # pyrekordbox attribute may or may not exist; be lenient.
    value = getattr(track, "ISRC", None)
    if value is None or not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def load_from_rekordbox(
    *,
    playlist_names: Iterable[str],
    db=None,
    hash_cache: HashCache | None = None,
) -> list[CanonicalTrack]:
    """Read the given playlist names from a Rekordbox DB.

    Parameters
    ----------
    playlist_names
        Must match exactly (case sensitive).
    db
        Open :class:`pyrekordbox.Rekordbox6Database`. When None, opens
        the working-copy DB via :func:`apps.shared.rekordbox_db.open_db`.
    hash_cache
        Optional :class:`HashCache`. Caller owns lifetime.

    Missing playlists are reported via :class:`KeyError` listing every
    missing name.
    """
    from apps.shared import rekordbox_db as rbdb  # lazy import -- optional dep

    close_after = False
    if db is None:
        # Phase 10 only reads from the working-copy DB (no .save() / .commit()
        # calls anywhere in apps/sync/usb). open_db() copies from the live DB
        # to the working copy if missing and returns a pyrekordbox handle we
        # use strictly for `get_playlist()` / `get_content()` queries.
        db = rbdb.open_db()
        close_after = True

    try:
        wanted = {n for n in playlist_names}
        playlists = {p.Name: p for p in db.get_playlist() if p.Name in wanted}
        missing = wanted - set(playlists)
        if missing:
            raise KeyError(f"Playlists not found in Rekordbox: {sorted(missing)}")

        # Build id -> raw track once. get_content() is expensive on large
        # libraries.
        id_to_track: dict[str, object] = {str(t.ID): t for t in db.get_content()}

        out: list[CanonicalTrack] = []
        for name, pl in playlists.items():
            songs = list(getattr(pl, "Songs", []) or [])
            songs.sort(key=lambda s: (getattr(s, "TrackNo", 0) or 0))
            for song in songs:
                cid = getattr(song, "ContentID", None)
                if cid is None:
                    continue
                track = id_to_track.get(str(cid))
                if track is None:
                    continue
                folder_path = getattr(track, "FolderPath", "") or ""
                if not folder_path or folder_path.startswith(
                    ("spotify:", "tidal:", "http://", "https://")
                ):
                    continue
                src = Path(folder_path).expanduser()
                if not src.exists():
                    # Broken link -- skip; Phase 1 reconcile handles these.
                    continue
                try:
                    size_bytes = src.stat().st_size
                except OSError:
                    continue
                isrc = _isrc_from_rb(track)
                duration_ms = _coerce_duration(getattr(track, "Length", None))
                digest = _compute_content_hash(src, hash_cache)
                sid = stable_id_str(
                    isrc=isrc,
                    fingerprint=None,
                    duration_ms=duration_ms,
                    size_bytes=size_bytes,
                    abs_path=str(src.resolve()),
                    mtime=src.stat().st_mtime,
                )
                artist = getattr(getattr(track, "Artist", None), "Name", "") or ""
                album = getattr(getattr(track, "Album", None), "Name", "") or ""
                out.append(
                    CanonicalTrack(
                        stable_id=sid,
                        playlist=name,
                        source_path=src,
                        title=getattr(track, "Title", "") or "",
                        artist=artist,
                        album=album,
                        duration_ms=duration_ms,
                        size_bytes=size_bytes,
                        isrc=isrc,
                        content_hash=digest,
                    )
                )
        return out
    finally:
        if close_after:
            try:
                db.close()
            except Exception:
                pass


def load_canonical_tracks(
    *,
    playlist_names: Iterable[str],
    use_shared_state: bool = False,
    hash_cache: HashCache | None = None,
    db=None,
) -> list[CanonicalTrack]:
    """Front-door loader. Routes to shim or (future) shared-state.

    When ``use_shared_state=True`` and the shared-state projection exists
    and carries the required fields, we would route there; until Phase 5
    publishes its reader (tracked in STATE.md), we always fall through to
    the RB shim.
    """
    if use_shared_state:
        # Phase 5 status: ``apps.shared.state`` ships the tracks,
        # playlists and playlist_memberships tables with a ``content_hash``
        # column on tracks and a ``stable_id`` primary key, but
        # ``apps.shared.state.ingest.rekordbox`` currently writes
        # ``content_hash=None`` (see rekordbox.py:215) and no reader
        # helper projects rows into :class:`CanonicalTrack`. Until
        # content_hash population lands and a reader exposes the
        # (track, playlist, membership) join in CanonicalTrack shape,
        # fall through to the RB shim. Tracked in STATE.md as a Phase
        # 5 follow-up (shared-state USB sync reader).
        pass
    return load_from_rekordbox(
        playlist_names=playlist_names,
        db=db,
        hash_cache=hash_cache,
    )


def group_by_playlist(
    tracks: list[CanonicalTrack],
) -> dict[str, list[CanonicalTrack]]:
    out: dict[str, list[CanonicalTrack]] = {}
    for t in tracks:
        out.setdefault(t.playlist, []).append(t)
    return out


def dedupe_preserving_playlists(
    tracks: list[CanonicalTrack],
) -> list[CanonicalTrack]:
    """Collapse duplicate stable_ids while recording every playlist hit.

    Not currently used (we copy once per unique file; the m3u8 emitter
    is the one that cares about per-playlist membership). Kept as a
    building block for the verifier.
    """
    seen: dict[str, CanonicalTrack] = {}
    for t in tracks:
        if t.stable_id not in seen:
            seen[t.stable_id] = t
    return list(seen.values())


__all__ = [
    "CanonicalTrack",
    "load_from_rekordbox",
    "load_canonical_tracks",
    "group_by_playlist",
    "dedupe_preserving_playlists",
]
