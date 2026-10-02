"""StateBackend protocol + InMemoryBackend reference implementation.

The FastAPI routes never talk to SQLite directly. They call methods on a
:class:`StateBackend` instance, which:

  * in tests, is :class:`InMemoryBackend` (fast, deterministic, no IO)
  * in production, will be a sqlite-backed impl wired against Phase 5's
    ``apps.shared.state`` primitives.

Phase 5 status: ``apps.shared.state`` now ships ``db.open_ro/open_rw``,
``writer.StateWriter``, ``events.EventBus`` and a tracks / playlists /
pairings / events schema. What is still missing for production wiring
is a dedicated ``apps.shared.state.webui_adapter`` module that exposes
this Protocol's reader methods (``list_tracks`` with filter/cursor,
``list_pairings``, ``get_queue`` for the M3 triage queues) on top of
those primitives, plus writer plumbing that maps PATCH/POST/DELETE to
``StateWriter`` calls with the correct ``Source`` + provenance
envelopes. Until that adapter lands, the daemon defaults to
:class:`InMemoryBackend` and the triage queues are gated by
``InMemoryBackend.mark_queue_unavailable``. Tracked in STATE.md as a
Phase 5 follow-up (webui sqlite adapter).

All methods are synchronous. FastAPI handles the thread pool.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, Protocol

# --- data models (dict-shaped; pydantic is a view layer) -----------------

QueueKind = Literal["dedup", "bad_beatgrid", "auto_cue"]
Source = Literal[
    "mik", "rekordbox", "djay", "serato", "traktor",
    "open-dj-tool", "manual", "inferred", "webui",
]

# Whether a field's value is real, measured-and-failed, or not yet measured.
FieldStatus = Literal["ok", "failed", "missing"]

DEFAULT_LIMIT: int = 200
MAX_LIMIT: int = 1000


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Provenance:
    """Provenance envelope per open-dj v0 strawman section 6 / OPEN-01c.

    ``status`` and ``reason`` carry native-analysis v1's failure half: an own
    lane that ran and could not measure is `failed` with the lane's named
    reason, and a lane with no record yet is `missing`. ``status`` is required
    for the reason spelled out on :class:`~apps.webui.server.models.ProvenanceOut`.

    ``source`` widened to `Source | str` when own analysis arrived: an own
    value's source is its canonical backend name (`own_beatgrid.inapp`), which
    is an open set the closed Literal cannot enumerate.
    """
    value: Any
    source: Source | str
    confidence: float | None
    modified_at: str
    status: FieldStatus
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Track:
    """Track with a merged-view flat facade plus per-field provenance."""
    stable_id: str
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration_ms: int | None = None
    bpm: float | None = None
    key: str | None = None
    rating: int | None = None
    tags: list[str] = field(default_factory=list)
    notes: str | None = None
    genre: str | None = None
    comments: str | None = None
    last_played_at: str | None = None
    # PREF-01: {"regular": float|None, "min": float|None, "max": float|None},
    # or None when never set for this track. Never fabricated.
    tempo_pref: dict[str, float | None] | None = None
    file_path: str | None = None
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    provenance: dict[str, Provenance] = field(default_factory=dict)
    # Which SOURCE each own lane-owned field came from, "" while every lane is
    # on rbx. Part of the etag: switching a lane changes bpm, key and the whole
    # provenance block without necessarily moving any timestamp, so timestamps
    # alone cannot separate the two representations.
    selection_tag: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["provenance"] = {k: v for k, v in data["provenance"].items()}
        return data


@dataclass
class TrackPlaylistHit:
    playlist_id: str
    name: str
    vendor: str
    positions: list[int]


@dataclass
class Playlist:
    playlist_id: str
    name: str
    vendor: str = "unknown"
    # Vendor-side playlist id (e.g. rekordbox djmdPlaylist.ID); None when the
    # backing store predates the column or the vendor has no such id.
    vendor_pl_id: str | None = None
    items: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    forbid_duplicates: bool = False


@dataclass
class PlaylistPage:
    """One ``limit``/``offset`` window of a playlist (LIBM-133).

    ``playlist`` carries the header; its ``items``/``item_ids`` hold only the
    window, and ``total`` counts every live member.
    """

    playlist: Playlist
    total: int


@dataclass
class Pairing:
    pairing_id: str
    from_stable_id: str
    to_stable_id: str
    direction: Literal["->", "<->"]
    source: Literal["manual", "learned", "ai"]
    notes: str | None
    snapshot: dict[str, object] | None = None
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)


@dataclass
class QueueItem:
    """One row in the M3 triage queues (dedup / bad_beatgrid / auto_cue)."""
    stable_id: str
    kind: QueueKind
    payload: dict[str, Any]


@dataclass
class TrackFilter:
    q: str | None = None
    bpm_min: float | None = None
    bpm_max: float | None = None
    key: str | None = None
    rating_min: int | None = None
    tag: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    show_deleted: bool = False


@dataclass
class Page:
    """Paginated response envelope."""
    items: list[Any]
    next_cursor: str | None


class BackendError(RuntimeError):
    """Base class for backend errors."""


class NotFoundError(BackendError):
    """Raised when an entity does not exist."""


class ConflictError(BackendError):
    """Raised when If-Match does not match the stored etag."""
    def __init__(self, current: dict[str, Any], etag: str) -> None:
        self.current = current
        self.etag = etag
        super().__init__("If-Match mismatch")


class BatchConflictError(BackendError):
    """Raised when one or more rows fail an atomic batch CAS precondition."""

    def __init__(self, conflicts: list[dict[str, str]]) -> None:
        self.conflicts = conflicts
        super().__init__("one or more If-Match values do not match")


def resolve_tempo_pref_write(patch_value: Any) -> dict[str, float | None] | None:
    """Validate + clamp a ``tempo_pref`` patch value (PREF-01).

    Shared by both backends so SqliteBackend and InMemoryBackend enforce the
    identical rule set. ``None`` clears the preference. Otherwise:

      * ``min >= max`` is rejected (a track must have a non-empty range).
      * ``regular`` is clamped into ``[min, max]`` rather than left out of
        bounds - the caller may be resubmitting an existing ``regular``
        alongside a newly narrowed range, and this is the one place both
        paths (a fresh edit and a range-only edit) go through.
    """
    if patch_value is None:
        return None
    regular = patch_value.get("regular")
    tmin = patch_value.get("min")
    tmax = patch_value.get("max")
    if tmin is not None and tmax is not None and tmin >= tmax:
        raise BackendError("tempo_pref.min must be less than tempo_pref.max")
    if regular is not None:
        if tmin is not None and regular < tmin:
            regular = tmin
        if tmax is not None and regular > tmax:
            regular = tmax
    return {"regular": regular, "min": tmin, "max": tmax}


class MyTagScopeConflictError(BackendError):
    """Raised when a destructive MyTag acknowledgement is no longer current."""

    def __init__(self, catalog_revision: str, affected_track_count: int) -> None:
        self.catalog_revision = catalog_revision
        self.affected_track_count = affected_track_count
        super().__init__("MyTag catalog scope changed")


class MyTagMergeConfirmationRequiredError(BackendError):
    """Raised when a rename would merge the source catalog into an existing tag."""

    def __init__(self, destination_name: str) -> None:
        self.destination_name = destination_name
        super().__init__("MyTag rename requires explicit merge confirmation")


@dataclass(frozen=True)
class TrackUpdate:
    """One compare-and-swap update belonging to an atomic track batch."""

    stable_id: str
    patch: dict[str, Any]
    expected_etag: str


def compute_mytag_catalog_revision(tracks: Sequence[Track]) -> str:
    """Return a stable quoted revision for the complete tag membership catalog."""
    catalog = [
        [track.stable_id, sorted(set(track.tags or []))]
        for track in sorted(tracks, key=lambda item: item.stable_id)
        if track.tags
    ]
    encoded = json.dumps(catalog, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f'"{sha256(encoded).hexdigest()}"'


def compute_library_revision(tracks: Sequence[Track]) -> str:
    """Return a stable revision for the rows exposed by the track library."""
    catalog = [
        [track.stable_id, asdict(track)]
        for track in sorted(tracks, key=lambda item: item.stable_id)
    ]
    return _library_revision_digest(catalog)


def compute_library_revision_summary(
    track_count: int,
    track_updated_at: str,
    field_updated_at: str,
    changelog_sequence: int,
) -> str:
    """Return a stable revision from the database's cheap change signals."""
    return _library_revision_digest([
        "summary",
        track_count,
        track_updated_at,
        field_updated_at,
        changelog_sequence,
    ])


def _library_revision_digest(catalog: object) -> str:
    encoded = json.dumps(
        catalog, separators=(",", ":"), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return f'"{sha256(encoded).hexdigest()}"'


class StateBackend(Protocol):
    """Narrow surface the web UI needs from the state layer."""
    def list_tracks(self, flt: TrackFilter) -> Page: ...
    def library_revision(self) -> str: ...
    def get_track(self, stable_id: str) -> Track: ...
    def get_tracks_bulk(self, stable_ids: Sequence[str]) -> dict[str, Track]: ...
    def get_file_paths_bulk(self, stable_ids: Sequence[str]) -> dict[str, str | None]: ...
    def list_playlists(self) -> list[Playlist]: ...
    def get_playlist(self, playlist_id: str) -> Playlist: ...
    def get_playlist_page(
        self, playlist_id: str, *, limit: int, offset: int,
    ) -> PlaylistPage: ...
    def list_track_playlists(self, stable_id: str) -> list[TrackPlaylistHit]: ...
    def list_pairings(self, *, from_stable_id: str | None = None,
                      to_stable_id: str | None = None,
                      source: str | None = None) -> list[Pairing]: ...
    def get_queue(self, kind: QueueKind) -> tuple[list[QueueItem], str | None]: ...
    def update_track(self, stable_id: str, patch: dict[str, Any], *,
                     expected_etag: str, source: Source = "webui",
                     mutation_guard: Callable[[], None] | None = None) -> Track: ...
    def update_tracks(self, updates: Sequence[TrackUpdate], *,
                      source: Source = "webui") -> list[Track]: ...
    def update_tag_members(self, old_name: str, new_name: str | None, *,
                           expected_catalog_revision: str,
                           expected_track_count: int,
                           confirm_merge: bool = False,
                           source: Source = "webui") -> int: ...
    def create_pairing(self, pairing: Pairing) -> Pairing: ...
    def delete_pairing(self, pairing_id: str, *, expected_etag: str) -> None: ...
    def stats(self) -> dict[str, Any]: ...
    def last_writer(self) -> tuple[str, str] | None: ...


class InMemoryBackend:
    """Deterministic, thread-safe in-memory StateBackend for tests + v1."""

    def __init__(self) -> None:
        self._mutex = threading.RLock()
        self._tracks: dict[str, Track] = {}
        self._playlists: dict[str, Playlist] = {}
        self._pairings: dict[str, Pairing] = {}
        self._queues: dict[QueueKind, tuple[list[QueueItem], str | None]] = {
            "dedup": ([], None),
            "bad_beatgrid": ([], None),
            "auto_cue": ([], None),
        }
        self._last_writer: tuple[str, str] | None = None

    def seed_track(self, track: Track) -> None:
        with self._mutex:
            self._tracks[track.stable_id] = track

    def seed_playlist(self, playlist: Playlist) -> None:
        with self._mutex:
            self._playlists[playlist.playlist_id] = playlist

    def seed_pairing(self, pairing: Pairing) -> None:
        with self._mutex:
            self._pairings[pairing.pairing_id] = pairing

    def seed_queue(self, kind: QueueKind, items: list[QueueItem],
                   note: str | None = None) -> None:
        with self._mutex:
            self._queues[kind] = (list(items), note)

    def mark_queue_unavailable(self, kind: QueueKind, note: str) -> None:
        """Used when the upstream Phase 6/7 tables are not merged yet."""
        with self._mutex:
            self._queues[kind] = ([], note)

    def list_tracks(self, flt: TrackFilter) -> Page:
        with self._mutex:
            tracks = list(self._tracks.values())
        if flt.q:
            needle = flt.q.casefold()
            tracks = [t for t in tracks
                      if (t.title and needle in t.title.casefold())
                      or (t.artist and needle in t.artist.casefold())]
        if flt.bpm_min is not None:
            tracks = [t for t in tracks if t.bpm is not None and t.bpm >= flt.bpm_min]
        if flt.bpm_max is not None:
            tracks = [t for t in tracks if t.bpm is not None and t.bpm <= flt.bpm_max]
        if flt.key:
            tracks = [t for t in tracks if t.key == flt.key]
        if flt.rating_min is not None:
            tracks = [t for t in tracks if t.rating is not None and t.rating >= flt.rating_min]
        if flt.tag:
            tracks = [t for t in tracks if flt.tag in (t.tags or [])]
        tracks.sort(key=lambda t: t.stable_id)
        limit = max(1, min(flt.limit, MAX_LIMIT))
        start = 0
        if flt.cursor:
            for i, t in enumerate(tracks):
                if t.stable_id > flt.cursor:
                    start = i
                    break
            else:
                start = len(tracks)
        page = tracks[start : start + limit]
        next_cursor = page[-1].stable_id if len(page) == limit else None
        return Page(items=page, next_cursor=next_cursor)

    def library_revision(self) -> str:
        with self._mutex:
            tracks = list(self._tracks.values())
        return compute_library_revision(tracks)

    def get_track(self, stable_id: str) -> Track:
        with self._mutex:
            track = self._tracks.get(stable_id)
        if track is None:
            raise NotFoundError(f"track not found: {stable_id}")
        return track

    def get_tracks_bulk(self, stable_ids: Sequence[str]) -> dict[str, Track]:
        """Found tracks keyed by stable_id; missing ids are simply absent
        (callers decide whether absence is an error -- playlist hydration
        treats a dangling membership as a loud failure)."""
        with self._mutex:
            return {
                sid: self._tracks[sid]
                for sid in stable_ids if sid in self._tracks
            }

    def get_file_paths_bulk(self, stable_ids: Sequence[str]) -> dict[str, str | None]:
        """``file_path`` only, for callers (playlist availability) that never
        touch the rest of the Track -- skips hydrating every EAV field for
        rows the caller was going to discard anyway."""
        with self._mutex:
            return {
                sid: self._tracks[sid].file_path
                for sid in stable_ids if sid in self._tracks
            }

    def list_playlists(self) -> list[Playlist]:
        with self._mutex:
            return list(self._playlists.values())

    def get_playlist(self, playlist_id: str) -> Playlist:
        with self._mutex:
            pl = self._playlists.get(playlist_id)
        if pl is None:
            raise NotFoundError(f"playlist not found: {playlist_id}")
        return pl

    def get_playlist_page(
        self, playlist_id: str, *, limit: int, offset: int,
    ) -> PlaylistPage:
        pl = self.get_playlist(playlist_id)
        window = slice(offset, offset + limit)
        return PlaylistPage(
            playlist=replace(pl, items=pl.items[window], item_ids=pl.item_ids[window]),
            total=len(pl.items),
        )

    def list_track_playlists(self, stable_id: str) -> list[TrackPlaylistHit]:
        with self._mutex:
            playlists = list(self._playlists.values())
        hits: list[TrackPlaylistHit] = []
        for pl in playlists:
            positions = [i for i, sid in enumerate(pl.items) if sid == stable_id]
            if positions:
                hits.append(TrackPlaylistHit(
                    playlist_id=pl.playlist_id,
                    name=pl.name,
                    vendor=pl.vendor,
                    positions=positions,
                ))
        hits.sort(key=lambda h: (h.name.casefold(), h.playlist_id))
        return hits

    def list_pairings(self, *, from_stable_id: str | None = None,
                      to_stable_id: str | None = None,
                      source: str | None = None) -> list[Pairing]:
        with self._mutex:
            out = list(self._pairings.values())
        if from_stable_id:
            out = [p for p in out if p.from_stable_id == from_stable_id]
        if to_stable_id:
            out = [p for p in out if p.to_stable_id == to_stable_id]
        if source:
            out = [p for p in out if p.source == source]
        out.sort(key=lambda p: p.created_at)
        return out

    def get_queue(self, kind: QueueKind) -> tuple[list[QueueItem], str | None]:
        with self._mutex:
            if kind not in self._queues:
                raise NotFoundError(f"unknown queue kind: {kind}")
            items, note = self._queues[kind]
            return list(items), note

    def update_track(self, stable_id: str, patch: dict[str, Any], *,
                     expected_etag: str, source: Source = "webui",
                     mutation_guard: Callable[[], None] | None = None) -> Track:
        try:
            return self.update_tracks(
                [TrackUpdate(stable_id, patch, expected_etag)], source=source,
                mutation_guard=mutation_guard,
            )[0]
        except BatchConflictError as exc:
            current = self.get_track(stable_id)
            raise ConflictError(current.to_dict(), exc.conflicts[0]["current_etag"]) from exc

    def update_tracks(self, updates: Sequence[TrackUpdate], *,
                      source: Source = "webui",
                      mutation_guard: Callable[[], None] | None = None) -> list[Track]:
        from .etag import compute_etag, strip_quotes
        with self._mutex:
            stable_ids = [update.stable_id for update in updates]
            if len(stable_ids) != len(set(stable_ids)):
                raise BackendError("batch contains duplicate stable_ids")
            current_rows: list[Track] = []
            conflicts: list[dict[str, str]] = []
            for update in updates:
                track = self._tracks.get(update.stable_id)
                if track is None:
                    raise NotFoundError(f"track not found: {update.stable_id}")
                current_rows.append(track)
                current_etag = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
                if strip_quotes(current_etag) != strip_quotes(update.expected_etag):
                    conflicts.append({"stable_id": update.stable_id, "current_etag": current_etag})
            if conflicts:
                raise BatchConflictError(conflicts)
            for update in updates:
                rating = update.patch.get("rating")
                if "rating" in update.patch and rating is not None and not (0 <= rating <= 5):
                    raise BackendError("rating must be between 0 and 5")
            now = _utcnow_iso()
            results: list[Track] = []
            for track, update in zip(current_rows, updates, strict=True):
                if not update.patch:
                    results.append(track)
                    continue
                updated = replace(track, updated_at=now)
                prov = dict(updated.provenance)
                if "rating" in update.patch:
                    updated.rating = update.patch["rating"]
                    prov["rating"] = Provenance(value=updated.rating, source=source,
                                                confidence=1.0, modified_at=now,
                                                status="ok")
                if "notes" in update.patch:
                    updated.notes = update.patch["notes"]
                    prov["notes"] = Provenance(value=updated.notes, source=source,
                                               confidence=1.0, modified_at=now,
                                               status="ok")
                if "genre" in update.patch:
                    updated.genre = update.patch["genre"]
                    prov["genre"] = Provenance(value=updated.genre, source=source,
                                               confidence=1.0, modified_at=now,
                                               status="ok")
                if "comments" in update.patch:
                    updated.comments = update.patch["comments"]
                    prov["comments"] = Provenance(value=updated.comments, source=source,
                                                  confidence=1.0, modified_at=now,
                                                  status="ok")
                if "tempo_pref" in update.patch:
                    updated.tempo_pref = resolve_tempo_pref_write(update.patch["tempo_pref"])
                    prov["tempo_pref"] = Provenance(value=updated.tempo_pref, source=source,
                                                    confidence=1.0, modified_at=now,
                                                    status="ok")
                if "file_path" in update.patch:
                    file_path = update.patch["file_path"]
                    if not isinstance(file_path, str) or not file_path:
                        raise BackendError("file_path must be a non-empty string")
                    updated.file_path = file_path
                    prov["file_path"] = Provenance(value=file_path, source=source,
                                                   confidence=1.0, modified_at=now,
                                                   status="ok")
                tags = list(updated.tags or [])
                if update.patch.get("tags_add"):
                    for tag in update.patch["tags_add"]:
                        if tag and tag not in tags:
                            tags.append(tag)
                if update.patch.get("tags_remove"):
                    tags = [tag for tag in tags if tag not in update.patch["tags_remove"]]
                if "tags_add" in update.patch or "tags_remove" in update.patch:
                    updated.tags = tags
                    prov["tags"] = Provenance(value=list(tags), source=source,
                                              confidence=1.0, modified_at=now,
                                              status="ok")
                updated.provenance = prov
                results.append(updated)
            changed = any(
                updated is not current for current, updated in zip(current_rows, results, strict=True)
            )
            previous_last_writer = self._last_writer
            if mutation_guard is not None:
                mutation_guard()
            try:
                for current, updated in zip(current_rows, results, strict=True):
                    if updated is not current:
                        self._tracks[updated.stable_id] = updated
                if changed:
                    self._last_writer = (source, now)
                if mutation_guard is not None:
                    mutation_guard()
            except Exception:
                for current in current_rows:
                    self._tracks[current.stable_id] = current
                self._last_writer = previous_last_writer
                raise
            return results

    def update_tag_members(self, old_name: str, new_name: str | None, *,
                           expected_catalog_revision: str,
                           expected_track_count: int,
                           confirm_merge: bool = False,
                           source: Source = "webui") -> int:
        """Atomically validate and mutate a MyTag catalog-wide scope."""
        from .etag import compute_etag
        with self._mutex:
            tracks = list(self._tracks.values())
            catalog_revision = compute_mytag_catalog_revision(tracks)
            members = [track for track in tracks if old_name in track.tags]
            if (catalog_revision != expected_catalog_revision
                    or len(members) != expected_track_count):
                raise MyTagScopeConflictError(catalog_revision, len(members))
            if (new_name is not None
                    and any(new_name in track.tags for track in tracks)
                    and not confirm_merge):
                raise MyTagMergeConfirmationRequiredError(new_name)
            updates = [
                TrackUpdate(
                    track.stable_id,
                    {"tags_remove": [old_name]} if new_name is None else {
                        "tags_add": [new_name], "tags_remove": [old_name],
                    },
                    compute_etag(track.stable_id, track.updated_at, track.selection_tag),
                )
                for track in members
            ]
            self.update_tracks(updates, source=source)
            return len(updates)

    def create_pairing(self, pairing: Pairing) -> Pairing:
        with self._mutex:
            for existing in self._pairings.values():
                if (existing.from_stable_id == pairing.from_stable_id
                        and existing.to_stable_id == pairing.to_stable_id
                        and existing.direction == pairing.direction):
                    if pairing.notes and pairing.notes != existing.notes:
                        merged_notes = f"{existing.notes or ''}\n{pairing.notes}".strip()
                        # Write-once, same as the snapshot-only branch below:
                        # a frozen open-time capture is never replaced, so
                        # merging notes cannot smuggle a recapture past that
                        # contract. Only a pairing with no snapshot yet
                        # accepts an incoming one.
                        snapshot = (
                            existing.snapshot if existing.snapshot is not None
                            else pairing.snapshot
                        )
                        updated = replace(
                            existing, notes=merged_notes, snapshot=snapshot,
                            updated_at=_utcnow_iso(),
                        )
                        self._pairings[existing.pairing_id] = updated
                        return updated
                    if pairing.snapshot is not None:
                        if existing.snapshot is not None:
                            return existing
                        updated = replace(
                            existing, snapshot=pairing.snapshot, updated_at=_utcnow_iso()
                        )
                        self._pairings[existing.pairing_id] = updated
                        return updated
                    return existing
            from .pairings_sqlite import raise_on_pairing_id_collision
            raise_on_pairing_id_collision(self._pairings.get(pairing.pairing_id), pairing)
            self._pairings[pairing.pairing_id] = pairing
            self._last_writer = (pairing.source, pairing.created_at)
            return pairing

    def delete_pairing(self, pairing_id: str, *, expected_etag: str) -> None:
        from .etag import compute_etag, strip_quotes
        with self._mutex:
            existing = self._pairings.get(pairing_id)
            if existing is None:
                raise NotFoundError(f"pairing not found: {pairing_id}")
            current = compute_etag(existing.pairing_id, existing.updated_at)
            if strip_quotes(current) != strip_quotes(expected_etag):
                raise ConflictError(
                    current={"pairing_id": existing.pairing_id,
                             "updated_at": existing.updated_at},
                    etag=current,
                )
            del self._pairings[pairing_id]
            self._last_writer = ("webui", _utcnow_iso())

    def stats(self) -> dict[str, Any]:
        with self._mutex:
            return {"tracks": len(self._tracks),
                    "playlists": len(self._playlists),
                    "pairings": len(self._pairings)}

    def last_writer(self) -> tuple[str, str] | None:
        with self._mutex:
            return self._last_writer


__all__ = [
    "BackendError", "ConflictError", "DEFAULT_LIMIT", "InMemoryBackend",
    "MAX_LIMIT", "NotFoundError", "Page", "Pairing", "Playlist", "Provenance",
    "QueueItem", "QueueKind", "Source", "StateBackend", "Track", "TrackFilter",
]
