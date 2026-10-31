"""Pydantic request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator
from pydantic.json_schema import SkipJsonSchema

from .models_grid_quality import GridQualityRowOut

# PREFLIGHT-01's boot-gate schemas live in their own module (models.py size budget);
# re-exported here so every existing import keeps working.
from .models_preflight import PreflightCheckOut, PreflightOut
from .models_tempo_pref import TempoPrefOut, TempoPrefPatch

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


class TrackLoudnessOut(BaseModel):
    """AGENT-31: integrated loudness from the loudness lane's projection, or None with why."""

    lufs: float | None
    #: ``error``: the loudness store itself is absent or has the wrong schema (not "unmeasured").
    status: Literal["ok", "failed", "missing", "error"]
    reason: str | None = None


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
    # Same field as RbMetaOut / listing: True = GET /artwork would 200,
    # False = would 404 ARTWORK_NOT_FOUND. None is kept in the type for wire
    # compatibility only: since the tag reader became a core dependency no
    # build answers it. Deck-load GET /tracks/{sid} carries this so
    # the browser can skip the img GET (same job as has_rb_mapping /
    # lyrics_available).
    artwork_available: bool | None
    #: AGENT-31: served by the single-track read only (None on listings), for the AutoDJ loudness match.
    loudness: TrackLoudnessOut | None = None


class QualityGradeOut(BaseModel):
    """The QLT letter (QLTSCAN-02, apps/shared/audio_grade.py).

    `checked` is false until a scan has measured the current file under the
    current rule: the letter then comes from bitrate and container alone and
    the badge draws it hollow. `letter` is null when the venue is unknown.
    `issues` are issue keys (clipping, dc_offset, ...) that put a dot on the
    badge. The full hover text is not on the row (it would break the row byte
    budget); the badge loads it from `GET /api/v1/audio-quality/{stable_id}`.
    """

    letter: Literal["A", "B", "C", "D"] | None
    checked: bool
    issues: list[str]


class QualityOut(BaseModel):
    """One rung of the venue ladder, or an honest unknown.

    venue/rank/kbps are null when the file could not be measured; `blurb`
    then carries the reason. The UI must never render a guessed rung.
    `grade` is set on track-list rows (QLTSCAN-02) and null elsewhere.
    """

    venue: str | None
    label: str
    rank: int | None
    of: int
    blurb: str
    kbps: int | None
    container: str
    lossless: bool
    grade: QualityGradeOut | None = None


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



_STANDARD_MISSING_REASON = {
    "key_status": "key not analyzed",
    "bpm_status": "bpm not analyzed",
    "loudness_status": "loudness_lufs not analyzed",
}
# The served value that proves an omitted status was ``ok``. Loudness has no
# value on the listing wire, so nothing could prove it and its status stays.
_LANE_VALUE_KEY = {"key_status": "key", "bpm_status": "bpm"}


def _lane_has_value(value: Any) -> bool:
    """Same predicate as ``laneHasValue`` in the browser's browser-row-wire.ts.

    The two must agree: the server omits ``ok`` exactly where the client will
    rebuild it, so an empty string counts as no value on both sides.
    """
    return value is not None and value != ""


def compact_listing_analysis_wire(data: dict[str, Any]) -> dict[str, Any]:
    """Omit listing bytes the client already reconstructs.

    Measured on the LIBM-137 500-row fixture (Thu 8 Oct 2026): serving
    key/bpm/loudness status plus reason added 185 bytes per row and the page
    landed at 1955 bytes per row, over the 1900 budget. The three stock
    missing reasons were 111 of those bytes. An ``ok`` status is omitted only
    when the lane's non-empty value is on the same row, because the value is what lets
    the client tell "ok, compacted" from "the server dropped the field". An
    ``ok`` with no value, and every loudness status, stays on the wire. A
    reason that is not the stock sentence (an unmapped lane's "no own analysis
    record") stays on the wire.
    """
    for status_key, standard in _STANDARD_MISSING_REASON.items():
        reason_key = status_key.replace("_status", "_reason")
        status = data.get(status_key)
        reason = data.get(reason_key)
        if reason is None or (status == "missing" and reason == standard):
            data.pop(reason_key, None)
        value_key = _LANE_VALUE_KEY.get(status_key)
        value_proves_ok = value_key is not None and _lane_has_value(data.get(value_key))
        if status is None or (status == "ok" and value_proves_ok):
            data.pop(status_key, None)
    return data


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
    # CHROME-02: same facts TrackRowOut carries, so All Tracks and search
    # classify an unmapped streaming row without an rb-meta FolderPath.
    is_streaming: bool
    streaming_provider: Literal["spotify", "tidal", "soundcloud", "unknown"] | None = None
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
    # LIBUX-34: the BPM hover's beatgrid method and confidence, the same
    # provenance TrackRowOut carries, so All Tracks and search match playlists.
    bpm_source: str | None = None
    bpm_method: str | None = None
    bpm_confidence: float | None = None
    bpm_confidence_error: str | None = None  # stored confidence not a number in [0, 1]
    # STANDALONE-03: empty analysis cells must state why. Same status/reason
    # fields TrackRowOut already carries for playlists; All Tracks used to drop
    # them when assembling TrackListItemOut, so the browser defaulted absent
    # bpm_status to "ok" and rendered a blank cell with no reason.
    # None is "not on the JSON". The route still passes the computed status;
    # compact_listing_analysis_wire drops the stock missing sentence, and an
    # ok status only where the served value proves it.
    key_status: Literal["ok", "failed", "missing", "available-not-selected"] | None = None
    key_reason: str | None = None
    bpm_status: Literal["ok", "failed", "missing", "available-not-selected"] | None = None
    bpm_reason: str | None = None
    loudness_status: Literal["ok", "failed", "missing", "available-not-selected"] | None = None
    loudness_reason: str | None = None

    @model_serializer(mode="wrap")
    def _compact_analysis_status(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            return compact_listing_analysis_wire(data)
        return data

    lyrics: LyricsRowSummaryOut | None = None
    grid_quality: GridQualityRowOut | None = None
    is_remix: bool = False
    is_radio_edit: bool = False
    # STANDALONE-05: inline genre for state-only rows; genre_reason names why
    # the cell is empty (no file tag vs no rekordbox genre).
    genre: str | None = None
    # GENRE-04: which provider the shown genre came from (user, beatport,
    # odj-fused, library-tag, ...); None when there is no genre.
    genre_source: str | None = None
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
    #: LIBM-178: non-music rows left out of THIS page (0 with include_non_music=true).
    non_music_hidden: int = 0


class TrackIndexItemOut(TrackListItemOut):
    """One library index row: a listing row without its per-row disk reads (LIBM-172).

    The preview strip, vocal regions, cover verdict and stem bundle summary are
    read off disk per row, which was 7.8 s of a cold 9,713-row index (stems
    another 1.2 s). They are never sent here (excluded and absent from the
    schema); the browser asks ``POST /library/row-assets`` for the rows in view.

    Also left out, because no library view reads them off an index row:
    ``provenance``, ``tags``, ``last_played_at``, ``tempo_pref``, ``created_at``,
    ``updated_at`` and the three optional-resource flags (``GET /tracks/{sid}``
    serves all of them). ``lyrics_available`` alone cost 2.9 s of a cold index,
    classifying every lyric file for ASR hallucinations.
    """

    provenance: SkipJsonSchema[dict[str, ProvenanceOut]] = Field(default={}, exclude=True)
    preview_b64: SkipJsonSchema[str | None] = Field(default=None, exclude=True)
    preview_max: SkipJsonSchema[int | None] = Field(default=None, exclude=True)
    vocals: SkipJsonSchema[dict[str, Any]] = Field(default_factory=dict, exclude=True)
    artwork_available: SkipJsonSchema[bool | None] = Field(default=None, exclude=True)
    artwork_status: SkipJsonSchema[str] = Field(default="unresolved", exclude=True)
    stems: SkipJsonSchema[dict[str, Any] | None] = Field(default=None, exclude=True)
    tags: SkipJsonSchema[list[str]] = Field(default_factory=list, exclude=True)
    last_played_at: SkipJsonSchema[str | None] = Field(default=None, exclude=True)
    tempo_pref: SkipJsonSchema[TempoPrefOut | None] = Field(default=None, exclude=True)
    created_at: SkipJsonSchema[str] = Field(default="", exclude=True)
    updated_at: SkipJsonSchema[str] = Field(default="", exclude=True)
    lyrics_available: SkipJsonSchema[bool] = Field(default=False, exclude=True)
    auto_cues_available: SkipJsonSchema[bool] = Field(default=False, exclude=True)
    stems_available: SkipJsonSchema[bool] = Field(default=False, exclude=True)
    #: CMDK-04: the Cmd K v2 bar searches comments across the whole collection from
    #: this index, so the index carries them (a string the backend already holds per
    #: track; no disk read). Listing rows (``GET /tracks``) do not.
    comments: str | None = None


class TrackIndexOut(BaseModel):
    """GET /tracks/index: every library row in one response (LIBM-171, LIBM-172).

    The library ``revision`` was read before the rows.
    """

    revision: str
    items: list[TrackIndexItemOut]
    #: LIBM-178: non-music rows left out of the index (0 with include_non_music=true).
    non_music_hidden: int = 0


class RowAssetOut(BaseModel):
    """The per-row disk reads a library index row leaves out (LIBM-172)."""

    preview_b64: str | None
    preview_max: int | None
    vocals: dict[str, Any]
    artwork_available: bool | None
    artwork_status: Literal["ok", "no_image_path", "unresolved", "file_missing"]
    stems: dict[str, Any]


class RowAssetsOut(BaseModel):
    """``assets`` holds every asked id that is a library track; ``pending`` lists
    strip-less ids the ahead-analysis drain was bumped for (ask again later)."""

    assets: dict[str, RowAssetOut]
    pending: list[str]


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
    genre_source: str | None = None
    genre_reason: str | None = None
    genre_guess: GenreGuessOut | None = None
    comments: str | None
    etag: str
    preview_b64: str | None
    preview_max: int | None
    file_availability: FileAvailabilityStatus
    file_exists: bool | None
    # Path this row's availability check looked at (rekordbox FolderPath, else
    # the state-layer file_path). The cloud icon titles it when unavailable.
    file_path: str | None = None
    is_streaming: bool
    # CHROME-02: which service streams this row (null when not streaming), so
    # an unmapped streaming row whose rb-meta never loads still shows its icon.
    streaming_provider: Literal["spotify", "tidal", "soundcloud", "unknown"] | None = None
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
    bpm_source: str | None = None
    bpm_method: str | None = None
    bpm_confidence: float | None = None
    bpm_confidence_error: str | None = None  # stored confidence not a number in [0, 1]
    loudness_status: Literal["ok", "failed", "missing", "available-not-selected"]
    loudness_reason: str | None
    lyrics: LyricsRowSummaryOut | None = None
    grid_quality: GridQualityRowOut | None = None
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
    from_stable_id: str = Field(min_length=1)
    to_stable_id: str = Field(min_length=1)
    direction: Literal["->", "<->"] = "->"
    source: Literal["manual", "learned", "ai"] = "manual"
    notes: str | None = Field(default=None, max_length=1000)
    snapshot: PairingSnapshot | None = None

    @model_validator(mode="after")
    def validate_snapshot_decks_match_pair(self) -> PairingCreate:
        if self.from_stable_id == self.to_stable_id:
            raise ValueError("a pairing needs two different tracks")
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
    # Live rows whose track_availability is present. Streaming-service rows
    # are not playable. Unchecked and absent rows stay in `tracks`.
    tracks_playable: int = 0
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
    #: UIURL-01: the browser address of the UI this process serves
    #: (``http://opendj.localhost:<port>/``), or null when it serves none.
    #: GET /api/v1/ui-url carries the same value with its loopback twin.
    ui_url: str | None
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
    #: Positive control: a real launchd-started app always has HOME (a real
    #: Windows process has USERPROFILE instead, ``OPS32_HOME_ENV_VAR``), so its
    #: absence means this field was never a real environment read (a stub, a
    #: pre-OPS-32 engine with no field at all, or a malformed body).
    process_env_home_present: bool


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
