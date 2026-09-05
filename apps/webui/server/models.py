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
    # Rekordbox djmdContent.DJPlayCount when hydrated via rb_vendor; 0 if unknown.
    play_count: int = 0
    created_at: str
    updated_at: str
    provenance: dict[str, ProvenanceOut] = {}
    # Whether GET /tracks/{sid}/rb-meta can resolve this row (live rekordbox
    # track_vendor_ids mapping AND djmdContent row). False for a locally
    # imported or djay-only track. See TrackListItemOut's longer note --
    # same flag, same semantics, now on the single-track shape too so the
    # deck-load path (GET /tracks/{sid}, never the listing row) can gate
    # hot-cue SAVE without guessing (PARITY-TODO, issue #736).
    has_rb_mapping: bool


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
    vocals: same four-status shape as /anlz (PVDI or demucs vocal-cache).
    stems: demucs bundle summary for the browser Stems column (V/I/D), or
    ``{status: none}`` when no local bundle exists.
    has_rb_mapping: whether GET /tracks/{sid}/rb-meta can resolve this row.
    False for a locally imported or djay-only track, whose rb-meta 404s BY
    CONTRACT; the browser skips the per-row fetch rather than provoke it.
    """

    preview_b64: str | None
    preview_max: int | None
    file_exists: bool
    quality: QualityOut
    vocals: dict[str, Any]
    stems: dict[str, Any]
    # Artwork facts are computed in the listing's existing bulk row assembly,
    # so TrackTable does not depend on IntersectionObserver hydration.
    artwork_available: bool | None
    artwork_status: Literal["ok", "no_image_path", "unresolved", "file_missing"]


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
    a row can be PATCHed directly with If-Match. ``vocals`` is the same
    four-status field as /anlz so library PreviewStrip blue bars paint
    without a per-row analysis fetch.
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
    # Unmatched Spotify placeholder (synthetic spotify-pending:* stable_id).
    # Distinct from generic streaming so the browser can light-green tint.
    spotify_pending: bool = False
    quality: QualityOut
    play_count: int = 0
    vocals: dict[str, Any]
    stems: dict[str, Any]
    # Whether GET /tracks/{sid}/rb-meta can resolve (see TrackListItemOut).
    has_rb_mapping: bool
    artwork_available: bool | None
    artwork_status: Literal["ok", "no_image_path", "unresolved", "file_missing"]


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


class HealthWaveformMaterialization(BaseModel):
    requested: str
    selected: str
    native_available: bool
    native_import_error: str | None = None


class HealthOut(BaseModel):
    status: str = "ok"
    state_db: HealthStateDb
    cloud: HealthCloud
    syncthing: HealthSyncthing | None = None
    waveform_materialization: HealthWaveformMaterialization
    bind_host: str
    version: str


class PreflightCheckOut(BaseModel):
    """One row of PREFLIGHT-01's boot gate (issue #771).

    ``status`` is never a two-way pass/fail: ``pending`` covers a check that
    genuinely could not be exercised (e.g. audio-access with no resolvable
    track anywhere in a small sample), which is an honest denominator, never
    a fabricated pass. ``remediation`` is null on a pass or a pending row and
    a real sentence on a fail.
    """

    id: str
    label: str
    status: Literal["pass", "fail", "pending"]
    detail: str
    remediation: str | None = None


class PreflightOut(BaseModel):
    """``GET /api/v1/preflight`` -- the ONE source of truth for the boot
    gate. ``status`` is ``fail`` iff any check is ``fail``; a ``pending``
    check never blocks it, because a check that could not be exercised is
    not a defect on its own.
    """

    status: Literal["pass", "fail"]
    checks: list[PreflightCheckOut]


__all__ = [
    "HealthCloud",
    "HealthOut",
    "HealthStateDb",
    "HealthSyncthing",
    "HealthWaveformMaterialization",
    "PairingCreate",
    "PairingOut",
    "PlaylistDetail",
    "PlaylistDiff",
    "PlaylistSummary",
    "PreflightCheckOut",
    "PreflightOut",
    "ProvenanceOut",
    "QueueItemOut",
    "QueueOut",
    "TrackListItemOut",
    "TrackOut",
    "TrackPatch",
    "TrackRowOut",
    "TracksPage",
]
