"""Pydantic request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

FileAvailabilityStatus = Literal[
    "present",
    "absent",
    "AVAILABILITY_PENDING",
    "streaming",
    "awaiting_volume",
]


class ProvenanceOut(BaseModel):
    """One field's value plus where it came from and whether it is real.

    ``status`` has NO DEFAULT on purpose (native-analysis v1, spec section 3
    and `.planning/REQUIREMENTS.md` NATIVE-04). A default of ``ok`` would let
    a caller omit the field and serialize a `failed` or `missing` own-analysis
    lane as a success, which is the exact silent-fallback shape this milestone
    exists to remove. Every construction site states its status, and the
    rekordbox/legacy boundary passes ``ok`` explicitly.
    """

    value: Any
    source: str
    confidence: float | None = None
    modified_at: str
    status: Literal["ok", "failed", "missing", "available-not-selected"]
    reason: str | None = None


class TempoPrefOut(BaseModel):
    """PREF-01: a track's user-set preferred tempo plus its playable range.

    Any of the three may be null (unset). Never fabricated on read - a track
    with no tempo_pref field row at all projects as a null ``TrackOut.tempo_pref``,
    not this shape with all-null members (see sqlite_backend._row_to_track).
    """

    regular: float | None = None
    min: float | None = None
    max: float | None = None


class TempoPrefPatch(BaseModel):
    regular: float | None = None
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _min_less_than_max(self) -> TempoPrefPatch:
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError("tempo_pref.min must be less than tempo_pref.max")
        return self


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
    # PREF-01: user-set preferred tempo + playable range, keyed by stable_id
    # (not vendor bpm). Null when never set for this track.
    tempo_pref: TempoPrefOut | None = None
    file_path: str | None = None
    # Rekordbox djmdContent.DJPlayCount plus Open DJ plays (PLAYS-01); 0 if neither.
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
    # Whether the matching optional GET would succeed, so the browser can skip
    # a fetch that would 404 (Chromium logs those unsuppressably). Same job as
    # has_rb_mapping: predict empty-state before issuing the request.
    lyrics_available: bool
    auto_cues_available: bool
    stems_available: bool
    # Same tri-state as RbMetaOut / listing: True = GET /artwork would 200,
    # False = would 404 ARTWORK_NOT_FOUND, None = would 503
    # ARTWORK_READER_UNAVAILABLE. Deck-load GET /tracks/{sid} carries this so
    # the browser can skip the img GET (same job as has_rb_mapping /
    # lyrics_available).
    artwork_available: bool | None


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


class GenreGuessOut(BaseModel):
    """GENRE-02: a JEV genre-family GUESS, served only while ``genre`` is empty; never a tag."""

    family: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: Literal["jev"]


class LyricsRowSummaryOut(BaseModel):
    """Per-row karaoke verdict summary for library listings."""

    verdict: str
    effective: str
    n_words: int | None = None
    n_lines: int | None = None
    has_words: bool
    pct_witness_red: float | None = None
    source: str | None = None
    language_iso3: str | None = None
    override: str | None = None


class CloudTransferOut(BaseModel):
    """A real in-process CloudSync asset operation for one library row."""

    direction: Literal["upload", "download"]
    bytes_transferred: int = Field(ge=0)
    # Null is reserved for a source that genuinely cannot report its total.
    # The current hydration upload path always supplies a numeric total.
    bytes_total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_progress_does_not_exceed_total(self) -> CloudTransferOut:
        if self.bytes_total is not None and self.bytes_transferred > self.bytes_total:
            raise ValueError("bytes_transferred must not exceed bytes_total")
        return self


class TrackListItemOut(TrackOut):
    """TrackOut + parity row fields (shared API contract item 1).

    preview_b64: base64 of uint8[120][3] interleaved [low, mid, hi] per
    column (null = no ANLZ analysis). preview_max: per-track max band value
    for client-side normalisation (never divide by 127 -- SPIKE-A1 gotcha 3).
    file_availability: typed disk-truth lane including AVAILABILITY_PENDING.
    file_exists: present/absent only; null while availability is pending.
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
    file_availability: FileAvailabilityStatus
    file_exists: bool | None
    # LIBUX-07: our own audio in non-local storage, not streaming and not
    # awaiting-volume. False (the default) is the honest common case.
    is_remote: bool = False
    # LIBUX-13: a durable remote object is recorded even when local audio
    # also exists. Unlike is_remote, this does not collapse local+cloud.
    has_remote_copy: bool
    # LIBUX-13: only present while this server process is moving real bytes.
    cloud_transfer: CloudTransferOut | None = None
    quality: QualityOut
    vocals: dict[str, Any]
    stems: dict[str, Any]
    # Artwork facts are computed in the listing's existing bulk row assembly,
    # so TrackTable does not depend on IntersectionObserver hydration.
    # artwork_available is inherited from TrackOut (same tri-state).
    artwork_status: Literal["ok", "no_image_path", "unresolved", "file_missing"]
    # Display-only MIK value. Null means the browser must render an empty
    # Energy cell and use energy_reason rather than inventing a number.
    energy: int | None
    energy_source: Literal["mik"] | None
    energy_reason: str
    lyrics: LyricsRowSummaryOut | None = None
    is_remix: bool = False
    is_radio_edit: bool = False
    # STANDALONE-05: inline genre for state-only rows; genre_reason names why
    # the cell is empty (missing tags extra vs no file tag vs no rekordbox genre).
    genre: str | None = None
    genre_reason: str | None = None
    genre_guess: GenreGuessOut | None = None


class LyricLineOut(BaseModel):
    """One cache-backed line timestamp in integer track milliseconds."""

    start_ms: int = Field(ge=0)
    text: str = Field(min_length=1)


class TrackLyricsOut(BaseModel):
    """Agent-native read model for cached line-synced lyrics only."""

    stable_id: str
    source: str
    lines: list[LyricLineOut] = Field(min_length=1)


class LyricsUnavailableOut(BaseModel):
    """The explicit cache-miss response for one track's lyrics timeline."""

    detail: str


class TracksPage(BaseModel):
    items: list[TrackListItemOut]
    next_cursor: str | None = None


class TrackPatch(BaseModel):
    rating: int | None = None
    tags_add: list[str] | None = None
    tags_remove: list[str] | None = None
    notes: str | None = None
    genre: str | None = None
    comments: str | None = None
    # PREF-01: send null to clear, omit to leave untouched (model_fields_set
    # distinguishes the two - see routes/tracks.py patch_track).
    tempo_pref: TempoPrefPatch | None = None

    @field_validator("rating")
    @classmethod
    def _rating_range(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 5):
            raise ValueError("rating must be between 0 and 5")
        return v


class TrackPlaylistOut(BaseModel):
    playlist_id: str
    name: str
    vendor: str
    positions: list[int]


class PlaylistSummary(BaseModel):
    playlist_id: str
    name: str
    vendor: str
    track_count: int
    # Members whose audio file is index-classified present (PERF-RB-01):
    # index-backed and may lag disk by up to the path index TTL; pending
    # members are excluded from this count.
    # ``-1`` means skipped (``GET /playlists?availability=skip``) for fast
    # tree paint; clients must not treat it as zero playable.
    available_count: int
    updated_at: str
    # Rekordbox tree position (flattened djmdPlaylist ParentID/Seq walk).
    # None when the playlist is not a rekordbox one or has no live
    # djmdPlaylist row - clients must not invent an order for those.
    seq: int | None = None
    forbid_duplicates: bool = False


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
    item_id: str | None = None
    title: str | None
    artist: str | None
    key: str | None
    bpm: float | None
    rating: int | None
    duration_ms: int | None
    genre: str | None
    genre_reason: str | None = None
    genre_guess: GenreGuessOut | None = None
    comments: str | None
    etag: str
    preview_b64: str | None
    preview_max: int | None
    file_availability: FileAvailabilityStatus
    file_exists: bool | None
    is_streaming: bool
    # LIBUX-07: our own audio in non-local storage. False when unset.
    is_remote: bool = False
    # LIBUX-13: true whenever track_locations records a live remote object,
    # including the local+cloud state that is_remote deliberately suppresses.
    has_remote_copy: bool
    # LIBUX-13: ephemeral operation state, distinct from durable presence.
    cloud_transfer: CloudTransferOut | None = None
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
    energy: int | None
    energy_source: Literal["mik"] | None
    energy_reason: str
    key_status: Literal["ok", "failed", "missing", "available-not-selected"]
    key_reason: str | None
    bpm_status: Literal["ok", "failed", "missing", "available-not-selected"]
    bpm_reason: str | None = None
    loudness_status: Literal["ok", "failed", "missing", "available-not-selected"]
    loudness_reason: str | None
    lyrics: LyricsRowSummaryOut | None = None
    is_remix: bool = False
    is_radio_edit: bool = False


class PlaylistDetail(BaseModel):
    playlist_id: str
    name: str
    vendor: str
    forbid_duplicates: bool = False
    # Full membership as stable_ids (always unfiltered -- the diff viewer
    # and reorder flows key off this).
    items: list[str]
    # Hydrated rows in membership order; ?available=true|false filters
    # THIS list only, never `items`.
    tracks: list[TrackRowOut]
    diff: PlaylistDiff


class PlaylistTracksPage(BaseModel):
    """Paginated playlist membership slice (PERF-UI-05, issue #3530).

    Hydrated rows in membership order for ``[offset:offset+limit]`` only.
    Agent parity: ``GET /api/v1/playlists/{playlist_id}/tracks``.
    """

    tracks: list[TrackRowOut]
    total: int
    next_offset: int | None = None


class PairingOut(BaseModel):
    pairing_id: str
    from_stable_id: str
    to_stable_id: str
    direction: Literal["->", "<->"]
    source: Literal["manual", "learned", "ai"]
    notes: str | None = None
    snapshot: PairingSnapshot | None = None
    created_at: str
    updated_at: str


class PairingTimestamp(BaseModel):
    unit: Literal["beats", "time"]
    value: float = Field(ge=0)


class PairingEqAdjust(BaseModel):
    band: Literal["low", "mid", "high"]
    value: float = Field(ge=0, le=1)


class PairingDeckSnapshot(BaseModel):
    deck_id: Literal[1, 2, 3, 4]
    stable_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    position_ms: int = Field(ge=0)
    timestamp: PairingTimestamp
    eq_adjusts: list[PairingEqAdjust]


class PairingSnapshot(BaseModel):
    version: Literal[1]
    beat_sync_max: bool
    decks: list[PairingDeckSnapshot] = Field(min_length=2, max_length=2)


class PairingCreate(BaseModel):
    from_stable_id: str
    to_stable_id: str
    direction: Literal["->", "<->"] = "->"
    source: Literal["manual", "learned", "ai"] = "manual"
    notes: str | None = Field(default=None, max_length=1000)
    snapshot: PairingSnapshot | None = None

    @model_validator(mode="after")
    def validate_snapshot_decks_match_pair(self) -> PairingCreate:
        if self.snapshot is None:
            return self
        snapshot_ids = {deck.stable_id for deck in self.snapshot.decks}
        pair_ids = {self.from_stable_id, self.to_stable_id}
        if len(snapshot_ids) != 2 or snapshot_ids != pair_ids:
            raise ValueError("snapshot decks must match the selected pairing tracks exactly")
        if len({deck.deck_id for deck in self.snapshot.decks}) != 2:
            raise ValueError("snapshot decks must use two distinct deck IDs")
        unit = "beats" if self.snapshot.beat_sync_max else "time"
        if any(deck.timestamp.unit != unit for deck in self.snapshot.decks):
            raise ValueError("snapshot timestamp units must match beat_sync_max")
        return self


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
    google_oauth_configured: bool
    #: OPS-32 round 4: this used to be `process_env_keys`, every name in this
    #: process's own `os.environ`. Round 4 (issue #2637/#2638 follow-on,
    #: Mon 14 Sep 2026) found that field readable by an UNAUTHENTICATED
    #: caller on a token-mode share host (`/health` is in
    #: `share_gate.EXEMPT_SUFFIXES` so cloudflared can probe it before a
    #: token is presented), which handed a public caller the full list of
    #: which services/secrets this install has configured. Narrowed to two
    #: fields, neither of which discloses anything beyond the OPS-32 gate's
    #: own need: which of the FORBIDDEN prefixes leaked (never the harmless
    #: majority of the environment), and one positive-control bit. Both
    #: required (no default) so a missing field still fails the gate fast
    #: rather than rendering as an empty/false pass.
    #:
    #: Sorted NAMES ONLY (never values) matching
    #: :data:`apps.webui.server.ops32_env_guard.OPS32_FORBIDDEN_ENV_PREFIXES`
    #: -- exists so a post-install rollout probe can verify the RUNNING
    #: engine's own environment via its self-report rather than reading it
    #: off the pid via KERN_PROCARGS2 -- measured Mon 14 Sep 2026 to read
    #: back zero env strings for the bundled Developer-ID signed python3, so
    #: procargs2 never actually measured the real packaged engine.
    process_env_forbidden_keys: list[str]
    #: Positive control: a real launchd-started app always has HOME, so its
    #: absence means this field was never a real environment read (a stub, a
    #: pre-OPS-32 engine with no field at all, or a malformed body).
    process_env_home_present: bool


class PreflightCheckOut(BaseModel):
    """One row of PREFLIGHT-01's boot gate (issue #771).

    ``status`` is never a two-way pass/fail: ``pending`` covers a check that
    genuinely could not be exercised (e.g. audio-access with no resolvable
    track anywhere in a small sample), which is an honest denominator, never
    a fabricated pass. ``remediation`` is null on a pass or a pending row and
    a real sentence on a fail.

    ``user_*`` fields carry plain-language copy for the boot gate (issue
    #2722). Admin/diagnostics views keep the technical ``label``/``detail``.
    """

    id: str
    label: str
    status: Literal["pass", "fail", "pending"]
    detail: str
    remediation: str | None = None
    user_label: str | None = None
    user_detail: str | None = None
    user_remediation: str | None = None
    #: How much this check MATTERS, which is a different axis from whether it
    #: passed (the maintainer, Wed 16 Sep 2026, after a fresh-Mac first run: "some
    #: checks aren't so important"). ``blocking`` means the app cannot
    #: usefully run until it passes, so the boot gate holds. ``advisory``
    #: means the app runs fine and the user is told, so the gate does not
    #: hold. The UI paints red for a failed blocking check and orange for a
    #: failed or unexercised advisory one, rather than red for everything.
    severity: Literal["blocking", "advisory"] = "blocking"
    #: One sentence answering "what do I do about this?", shown on hover.
    #: Distinct from ``remediation``: that is the fix for a FAILURE, this is
    #: present on every row including passes, so a user can ask what a row
    #: means without having to break it first.
    explainer: str | None = None


class PreflightOut(BaseModel):
    """``GET /api/v1/preflight`` -- the ONE source of truth for the boot
    gate. ``status`` is ``fail`` iff a check that is ``severity: blocking``
    is ``fail``; a ``pending`` check never blocks it, because a check that
    could not be exercised is not a defect on its own, and an ``advisory``
    check never blocks it either, because the app runs without it.

    ``advisories`` counts the non-blocking rows the user should still see,
    so a caller can distinguish "everything is fine" from "running, with
    things worth telling you" without recomputing severity for itself.
    """

    status: Literal["pass", "fail"]
    advisories: int = 0
    checks: list[PreflightCheckOut]


__all__ = [
    "CloudTransferOut",
    "HealthCloud",
    "HealthOut",
    "HealthStateDb",
    "HealthSyncthing",
    "HealthWaveformMaterialization",
    "LyricLineOut",
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
    "TrackLyricsOut",
    "TrackOut",
    "TrackPatch",
    "TrackRowOut",
    "TracksPage",
]
