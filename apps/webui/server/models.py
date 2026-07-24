"""Pydantic request/response schemas."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ProvenanceOut(BaseModel):
    value: Any
    source: str
    confidence: float | None = None
    modified_at: str


class TrackOut(BaseModel):
    stable_id: str
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration_ms: int | None = None
    bpm: float | None = None
    key: str | None = None
    rating: int | None = None
    tags: list[str] = []
    notes: str | None = None
    last_played_at: str | None = None
    file_path: str | None = None
    created_at: str
    updated_at: str
    provenance: dict[str, ProvenanceOut] = {}


class QualityOut(BaseModel):
    """One rung of the venue ladder, or an honest unknown.

    venue/rank/kbps are null when the file could not be measured; `blurb`
    then carries the reason. The UI must never render a guessed rung.
    """

    venue: str | None
    label: str
    rank: int | None
    of: int
    blurb: str
    kbps: int | None
    container: str
    lossless: bool


class QualityRungOut(BaseModel):
    """One legend entry from apps.shared.audio_quality.ladder()."""

    rank: int
    key: str
    label: str
    blurb: str


class TrackListItemOut(TrackOut):
    """TrackOut + parity row fields (shared API contract item 1).

    preview_b64: base64 of uint8[120][3] interleaved [low, mid, hi] per
    column (null = no ANLZ analysis). preview_max: per-track max band value
    for client-side normalisation (never divide by 127 -- SPIKE-A1 gotcha 3).
    file_exists: disk truth from the bulk-cached stat pass (FR-1 item 4).
    quality: venue rung from apps.shared.audio_quality (same stat pass, so
    no extra cost per row); venue/rank are null when it cannot be measured.
    """

    preview_b64: str | None
    preview_max: int | None
    file_exists: bool
    quality: QualityOut


class TracksPage(BaseModel):
    items: list[TrackListItemOut]
    next_cursor: str | None = None


class TrackPatch(BaseModel):
    rating: int | None = None
    tags_add: list[str] | None = None
    tags_remove: list[str] | None = None
    notes: str | None = None

    @field_validator("rating")
    @classmethod
    def _rating_range(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 5):
            raise ValueError("rating must be between 0 and 5")
        return v


class PlaylistSummary(BaseModel):
    playlist_id: str
    name: str
    vendor: str
    track_count: int
    # Members whose audio file exists on disk (FR-1 item 4): lets the tree
    # hide all-broken playlists and render "29 (3 broken)" style counts.
    available_count: int
    updated_at: str
    # Rekordbox tree position (flattened djmdPlaylist ParentID/Seq walk).
    # None when the playlist is not a rekordbox one or has no live
    # djmdPlaylist row - clients must not invent an order for those.
    seq: int | None = None


class PlaylistDiff(BaseModel):
    rb_only: list[str] = []
    djay_only: list[str] = []
    both: list[str] = []
    conflicts: list[dict[str, Any]] = []


class TrackRowOut(BaseModel):
    """Hydrated playlist track row (shared API contract item 4).

    Returned by playlist detail in membership order so the browser table
    renders without the old 29x per-row GET fan-out. ``etag`` is the same
    quoted sha1 the single-track endpoints emit (etag.py conventions), so
    a row can be PATCHed directly with If-Match.
    """

    stable_id: str
    title: str | None
    artist: str | None
    key: str | None
    bpm: float | None
    rating: int | None
    duration_ms: int | None
    genre: str | None
    comments: str | None
    etag: str
    preview_b64: str | None
    preview_max: int | None
    file_exists: bool
    is_streaming: bool
    quality: QualityOut


class PlaylistDetail(BaseModel):
    playlist_id: str
    name: str
    vendor: str
    # Full membership as stable_ids (always unfiltered -- the diff viewer
    # and reorder flows key off this).
    items: list[str]
    # Hydrated rows in membership order; ?available=true|false filters
    # THIS list only, never `items`.
    tracks: list[TrackRowOut]
    diff: PlaylistDiff


class PairingOut(BaseModel):
    pairing_id: str
    from_stable_id: str
    to_stable_id: str
    direction: Literal["->", "<->"]
    source: Literal["manual", "learned", "ai"]
    notes: str | None = None
    created_at: str
    updated_at: str


class PairingCreate(BaseModel):
    from_stable_id: str
    to_stable_id: str
    direction: Literal["->", "<->"] = "->"
    source: Literal["manual", "learned", "ai"] = "manual"
    notes: str | None = Field(default=None, max_length=1000)


class QueueItemOut(BaseModel):
    stable_id: str
    kind: str
    payload: dict[str, Any]


class QueueOut(BaseModel):
    items: list[QueueItemOut]
    note: str | None = None


class HealthStateDb(BaseModel):
    path: str
    tracks: int
    playlists: int
    pairings: int
    last_writer_hostname: str | None = None
    last_writer_at: str | None = None


class HealthCloud(BaseModel):
    lock_holder: dict[str, Any] | None = None
    last_wal_ship_at: str | None = None
    last_wal_ship_bytes: int | None = None


class HealthSyncthing(BaseModel):
    peers_connected: int = 0
    folder_state: str = "unknown"
    last_scan_at: str | None = None


class HealthOut(BaseModel):
    status: str = "ok"
    state_db: HealthStateDb
    cloud: HealthCloud
    syncthing: HealthSyncthing | None = None
    bind_host: str
    version: str


__all__ = [
    "HealthCloud", "HealthOut", "HealthStateDb", "HealthSyncthing",
    "PairingCreate", "PairingOut", "PlaylistDetail", "PlaylistDiff",
    "PlaylistSummary", "ProvenanceOut", "QueueItemOut", "QueueOut",
    "TrackListItemOut", "TrackOut", "TrackPatch", "TrackRowOut",
    "TracksPage",
]
