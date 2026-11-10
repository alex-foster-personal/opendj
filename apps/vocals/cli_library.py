"""Vocal library model, classification and trickle queue policy (split from
``apps.vocals.cli``).

-Claude
"""

from __future__ import annotations

import logging
import sqlite3
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from apps.shared.platform_paths import (
    MappedPath,
    PathMap,
    load_path_map,
    resolve_asset_path,
)
from apps.shared.state import locations as state_locations
from apps.vocals import cache as vcache
from apps.vocals import from_stems as vfrom_stems

# ----- CFG ---------------------------------------------------------------------
DEMUCS_REALTIME_FACTOR: float = 1.36  # SPIKE-B2 measured CPU rate (honest band 0.8-1.4x)
_SQL_CHUNK: int = 500  # keep IN (...) under SQLite's var cap

log = logging.getLogger(__name__)

CATEGORY_PVDI = "pvdi"
CATEGORY_CACHED = "cached_demucs"
CATEGORY_MISSING = "missing_file"
CATEGORY_MISSING_ANALYSIS = "missing_analysis"
CATEGORY_TODO = "todo"
CATEGORY_STEM_DERIVABLE = "stem_derivable"
CATEGORY_BROKEN_CACHE = "broken_vocal_cache"


@dataclass(frozen=True)
class Ctx:
    """Resolved data locations for one invocation (--data-dir aware)."""

    data_dir: Path
    state_db_path: Path | None = None

    @property
    def state_db(self) -> Path:
        if self.state_db_path is not None:
            return self.state_db_path
        return self.data_dir / "state" / "state.db"

    @property
    def master_db(self) -> Path:
        return self.data_dir / "master.plain.db"

    @property
    def cache_dir(self) -> Path:
        return vcache.cache_dir(self.data_dir)


@dataclass
class VocalTrack:
    """One rekordbox-mapped track with everything scan/trickle needs."""

    stable_id: str
    vendor_id: str
    title: str
    length_s: int
    folder_path: str | None
    analysis_data_path: str | None
    audio_path: Path | None
    audio_on_disk: bool
    category: str = ""


# ----- db plumbing ---------------------------------------------------------------


def _open_ro(path: Path, label: str) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"{label} missing on disk: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _resolve(path: str, *, path_map: PathMap) -> Path | None:
    """Resolve a state.db/rekordbox path via the shared platform resolver.

    ``None`` is the load-bearing "unmapped" state (a foreign-absolute path,
    e.g. a Mac FolderPath read on Windows with no path-map entry, or a
    streaming URI) -- never a fabricated ``Path`` that happens not to
    exist. Windows portability plan section 3.1."""
    mapped: MappedPath = resolve_asset_path(path, path_map=path_map)
    return mapped.resolved


def _resolve_share_path(path: str) -> Path:
    """Resolve a local path, rejecting unsafe or unmapped source paths."""
    mapped = resolve_asset_path(path)
    if mapped.resolved is None:
        raise ValueError(mapped.reason)
    return mapped.resolved


def _chunks(seq: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ----- track loading -------------------------------------------------------------


def rekordbox_mappings(ctx: Ctx, playlist: str | None) -> dict[str, tuple[str, str]]:
    """vendor_id -> (stable_id, title) for live rekordbox-mapped tracks.

    Reads state.db ONLY, so a caller can tell a state.db failure apart from
    a failure of the rekordbox copy (:func:`master_content_rows`).
    """
    state = _open_ro(ctx.state_db, "STATE_DB")
    try:
        if playlist is not None:
            pl_rows = state.execute(
                "SELECT playlist_id, name FROM playlists WHERE name = ? AND deleted_at IS NULL",
                (playlist,),
            ).fetchall()
            if not pl_rows:
                names = [
                    r[0]
                    for r in state.execute(
                        "SELECT DISTINCT name FROM playlists WHERE deleted_at IS NULL ORDER BY name"
                    ).fetchall()
                ]
                from apps.vocals.errors import UnknownPlaylistError

                raise UnknownPlaylistError(name=playlist, known=names)
            pl_ids = [r[0] for r in pl_rows]
            member_ids: set[str] = set()
            for chunk in _chunks(pl_ids, _SQL_CHUNK):
                marks = ",".join("?" * len(chunk))
                member_ids.update(
                    r[0]
                    for r in state.execute(
                        "SELECT DISTINCT stable_id FROM playlist_memberships "
                        f"WHERE playlist_id IN ({marks}) AND deleted_at IS NULL",
                        chunk,
                    ).fetchall()
                )
        # ADR 08 point 5 / round 2 finding 4b: a tombstoned track must not
        # enter the vocals trickle queue.
        mappings = state.execute(
            "SELECT t.stable_id, v.vendor_id, COALESCE(t.title, '') "
            "FROM tracks t "
            "JOIN track_vendor_ids v "
            "  ON v.stable_id = t.stable_id AND v.vendor = 'rekordbox' "
            "WHERE t.deleted_at IS NULL"
        ).fetchall()
    finally:
        state.close()

    if playlist is not None:
        mappings = [m for m in mappings if m[0] in member_ids]

    return {str(vid): (sid, title) for sid, vid, title in mappings}


def master_content_rows(ctx: Ctx, vendor_ids: list[str]) -> list[tuple[str, str, int, str | None, str | None]]:
    """Live djmdContent rows for ``vendor_ids``. Reads master.plain.db ONLY."""
    master = _open_ro(ctx.master_db, "MASTER_DB")
    rows: list[tuple[str, str, int, str | None, str | None]] = []
    try:
        for chunk in _chunks(vendor_ids, _SQL_CHUNK):
            marks = ",".join("?" * len(chunk))
            rows.extend(
                master.execute(
                    "SELECT ID, Title, Length, FolderPath, AnalysisDataPath "
                    "FROM djmdContent "
                    f"WHERE ID IN ({marks}) AND +rb_local_deleted = 0",
                    chunk,
                ).fetchall()
            )
    finally:
        master.close()
    return rows


def tracks_from_rows(
    ctx: Ctx,
    by_vendor: dict[str, tuple[str, str]],
    rows: list[tuple[str, str, int, str | None, str | None]],
) -> list[VocalTrack]:
    """Join the two halves into tracks, resolving each audio path."""
    path_map = load_path_map(ctx.data_dir)
    tracks: list[VocalTrack] = []
    for vendor_id, title, length_s, folder_path, adp in rows:
        stable_id, state_title = by_vendor[str(vendor_id)]
        audio: Path | None = None
        on_disk = False
        if folder_path:
            audio = _resolve(str(folder_path), path_map=path_map)
            on_disk = audio is not None and audio.is_file()
        tracks.append(
            VocalTrack(
                stable_id=stable_id,
                vendor_id=str(vendor_id),
                title=str(title or state_title),
                length_s=int(length_s) if length_s is not None else 0,
                folder_path=str(folder_path) if folder_path else None,
                analysis_data_path=str(adp) if adp else None,
                audio_path=audio,
                audio_on_disk=on_disk,
            )
        )
    tracks.sort(key=lambda t: t.stable_id)
    return tracks


def load_tracks(ctx: Ctx, playlist: str | None) -> list[VocalTrack]:
    """All rekordbox-mapped tracks (optionally one playlist's members)."""
    by_vendor = rekordbox_mappings(ctx, playlist)
    return tracks_from_rows(ctx, by_vendor, master_content_rows(ctx, list(by_vendor)))


def load_state_tracks(ctx: Ctx, stable_ids: Iterable[str] | None = None) -> list[VocalTrack]:
    """Every live track (or just ``stable_ids``) with its local audio, read
    from state.db only.

    from-stems needs only the app's own stable id and an audio file, so it
    depends on neither the decrypted rekordbox copy (``master.plain.db``: the
    packaged app has no such file, and requiring it failed every library
    refresh) nor a rekordbox vendor mapping (a folder-imported track has
    none, and requiring one left a library with no rekordbox without vocals
    forever). Audio is resolved the way the library listing resolves it.
    """
    state = _open_ro(ctx.state_db, "STATE_DB")
    try:
        select = "SELECT stable_id, COALESCE(title, ''), duration_ms FROM tracks WHERE deleted_at IS NULL"
        if stable_ids is None:
            rows = state.execute(select).fetchall()
        else:
            rows = []
            for chunk in _chunks(list(stable_ids), _SQL_CHUNK):
                marks = ",".join("?" * len(chunk))
                rows.extend(state.execute(f"{select} AND stable_id IN ({marks})", chunk).fetchall())
        ids = [str(sid) for sid, _title, _duration_ms in rows]
        audio = state_locations.bulk_local_audio_paths(state, ids, path_map=load_path_map(ctx.data_dir))
    finally:
        state.close()
    tracks = [
        VocalTrack(
            stable_id=str(sid),
            vendor_id="",
            title=str(title),
            length_s=int(duration_ms) // 1000 if duration_ms else 0,
            folder_path=None,
            analysis_data_path=None,
            audio_path=audio.get(str(sid)),
            audio_on_disk=audio.get(str(sid)) is not None,
        )
        for sid, title, duration_ms in rows
    ]
    tracks.sort(key=lambda t: t.stable_id)
    return tracks


# ----- classification --------------------------------------------------------------


def pvdi_present(path_2ex: Path) -> bool:
    """Cheap PVDI probe: seek-walk the PMAI section headers only (a scan
    over ~10k .2EX files must not read ~20 GB of full payloads)."""
    with path_2ex.open("rb") as fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] != b"PMAI":
            raise ValueError(f"not an ANLZ container: {path_2ex}")
        head_len, file_len = struct.unpack(">II", head[4:12])
        off = head_len
        while off + 12 <= file_len:
            fh.seek(off)
            section = fh.read(12)
            if len(section) < 12:
                break
            if section[:4] == b"PVDI":
                return True
            total_len = struct.unpack(">I", section[8:12])[0]
            if total_len <= 0:
                raise ValueError(f"corrupt section length in {path_2ex}")
            off += total_len
    return False


def _anlz_data_file(track: VocalTrack, path_map: PathMap) -> Path | None:
    """Return the local ANLZ .DAT required before ``/anlz`` can serve a track."""
    if track.analysis_data_path is None:
        return None
    resolved = _resolve(track.analysis_data_path, path_map=path_map)
    if resolved is None or not resolved.is_file():
        return None
    return resolved


def classify(
    ctx: Ctx,
    tracks: list[VocalTrack],
    *,
    stem_roots: Sequence[Path] | None = None,
) -> None:
    """Assign each track exactly one category (pvdi wins over everything:
    an analyzed track is covered even if its audio has since moved)."""
    path_map = load_path_map(ctx.data_dir)
    stem_candidates: list[VocalTrack] = []
    for tr in tracks:
        anlz_data = _anlz_data_file(tr, path_map)
        if anlz_data is None:
            if tr.audio_on_disk:
                tr.category = CATEGORY_MISSING_ANALYSIS
            else:
                tr.category = CATEGORY_MISSING
            continue

        twoex = anlz_data.with_suffix(".2EX")
        if twoex.is_file() and pvdi_present(twoex):
            tr.category = CATEGORY_PVDI
            continue
        try:
            entry = vcache.load_valid_entry(vcache.cache_path(ctx.data_dir, tr.stable_id), tr.audio_path)
        except ValueError as exc:
            log.warning("broken vocal-cache for %s during classify: %s", tr.stable_id, exc)
            tr.category = CATEGORY_BROKEN_CACHE
            continue
        if entry is not None:
            tr.category = CATEGORY_CACHED
            continue
        if not tr.audio_on_disk:
            tr.category = CATEGORY_MISSING
            continue
        stem_candidates.append(tr)

    loadable = vfrom_stems.loadable_bundle_ids(
        [t.stable_id for t in stem_candidates],
        stems_dir=vfrom_stems.stems_dir(ctx.data_dir),
        roots=stem_roots,
    )
    for tr in stem_candidates:
        if tr.stable_id in loadable:
            tr.category = CATEGORY_STEM_DERIVABLE
        else:
            tr.category = CATEGORY_TODO


# ----- trickle queue ordering -------------------------------------------------------


def best_playlist_rank(ctx: Ctx, tracks: list[VocalTrack]) -> dict[str, tuple[int, str]]:
    """stable_id -> (largest on-disk member count over its playlists, that
    playlist's name). Tracks in no playlist rank (0, '')."""
    on_disk_ids = {t.stable_id for t in tracks if t.audio_on_disk}
    state = _open_ro(ctx.state_db, "STATE_DB")
    try:
        rows = state.execute(
            "SELECT m.playlist_id, p.name, m.stable_id "
            "FROM playlist_memberships m "
            "JOIN playlists p ON p.playlist_id = m.playlist_id "
            "WHERE m.deleted_at IS NULL AND p.deleted_at IS NULL"
        ).fetchall()
    finally:
        state.close()

    members: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for pl_id, name, sid in rows:
        members.setdefault(pl_id, []).append(sid)
        names[pl_id] = name
    counts = {pl_id: sum(1 for sid in sids if sid in on_disk_ids) for pl_id, sids in members.items()}

    rank: dict[str, tuple[int, str]] = {}
    for pl_id, sids in members.items():
        for sid in sids:
            if counts[pl_id] > rank.get(sid, (0, ""))[0]:
                rank[sid] = (counts[pl_id], names[pl_id])
    return rank


def order_todo(todo: list[VocalTrack], rank: dict[str, tuple[int, str]]) -> list[VocalTrack]:
    """Priority: playlists with most on-disk members first, then shortest
    first (fast wins early), then stable_id for determinism."""
    return sorted(
        todo,
        key=lambda t: (
            -rank.get(t.stable_id, (0, ""))[0],
            t.length_s == 0,
            t.length_s,
            t.stable_id,
        ),
    )


def track_fits_budget(*, elapsed_s: float, budget_s: float, length_s: float, rate: float) -> bool:
    """True when ``length_s * rate`` fits in the remaining wall-clock budget."""
    remaining = max(0.0, budget_s - elapsed_s)
    return (length_s * rate) <= remaining


def _trickle_demucs_candidates(tracks: list[VocalTrack], *, demucs_even_if_stems: bool) -> list[VocalTrack]:
    cats = (CATEGORY_TODO, CATEGORY_STEM_DERIVABLE) if demucs_even_if_stems else (CATEGORY_TODO,)
    return [t for t in tracks if t.category in cats]
