"""Bulk row hydration for the browser listing read model (T3b S3 / C5).

Verbatim move of the ``rb_vendor.py`` "bulk row hydration" cluster
(``.planning/t3b-decomposition-map.md`` item 15, ``services/track_rows.py``
target -- landed here under ``rb_vendor_pkg`` because T3b wave 0/1 (S0's
``opendj/adapters/rekordbox`` package + facade) has not merged yet; the
facade re-export in ``rb_vendor.py`` keeps every caller unchanged either
way). ``build_track_rows`` joins state.db, master.plain.db, ETags, and stem
summaries into the browser read model -- contract items 1-4.

The two sideways imports the map calls out (``.etag.compute_etag`` and
``.stem_artifacts.bulk_stem_summaries``, previously function-local at
``rb_vendor.py:1152-1153``) are promoted to top-level here: neither module
imports back into ``rb_vendor`` or this package, so there is no cycle.

The path/DB helpers and the ``RbContent``/``RbRowMeta`` dataclasses used to
be reached through a function-local ``from .. import rb_vendor``, because
``rb_vendor.py`` still owned them and imports ran both ways. T3b wave 4 moved
them to ``apps/adapters/rekordbox/`` (config, errors, models, paths), so the
references are ordinary top-level imports and the deferred lookups are gone.

``config`` is imported as a module, never ``from config import STATE_DB``:
those constants are rebindable overrides and a by-value import would freeze
whichever value was current at import time. See that module's docstring.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox.errors import _open_ro
from apps.adapters.rekordbox.models import RbContent, RbRowMeta
from apps.adapters.rekordbox.paths import (
    bulk_local_artwork_available,
    is_streaming_path,
    local_artwork_available,
    resolve_asset_path,
)
from apps.analysis_waveform.local_waveform import local_preview_strip
from apps.cloud import transfer_status
from apps.genre_infer import jev_store
from apps.lyrics import store as lyric_store
from apps.shared import audio_quality, remote_status
from apps.shared._tagreader import HAS_TAG_READER
from apps.shared.mik_energy import readable_mik_energy
from apps.shared.platform_paths import AssetResolver, is_streaming_row
from apps.shared.state import play_log

from ..etag import compute_etag
from ..stem_artifacts import bulk_stem_summaries
from .anlz import preview_strip, vocals_for_content
from .availability import (
    PENDING,
    PROBE_BUDGET_ROW_HYDRATION,
    PROBE_BUDGET_TREE_SUMMARY,
    AvailabilityProbeMode,
    FileAvailabilityStatus,
    PathProbeResult,
    ProbeBudget,
    bulk_file_exists,
    bulk_file_size,
    bulk_probe_paths,
    classify_rows,
    local_paths,
    status_to_file_exists,
)

if TYPE_CHECKING:
    from apps.engine_core.jobs.store import JobStore

# Private to this module's chunking helper alone (moved from rb_vendor.py's
# preamble, where it had exactly one use site: _chunked's default arg, which
# is evaluated at def time and so cannot reach across the rb_vendor/
# rb_vendor_pkg boundary without reintroducing the cycle described above).
_SQL_CHUNK: int = 500  # keep IN (...) under SQLite's var cap

GENRE_REASON_TAG_READER_MISSING = (
    "genre unavailable: the tinytag tag reader is not importable (reinstall: uv sync)"
)
GENRE_REASON_NO_FILE_TAG = "no genre tag in file metadata"
GENRE_REASON_NO_REKORDBOX_GENRE = "no genre in rekordbox"


def _chunked(seq: Sequence[str], size: int = _SQL_CHUNK) -> Iterator[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def bulk_rb_meta(
    stable_ids: Sequence[str], *, state_db_path: Path | None = None
) -> dict[str, RbRowMeta]:
    """Bulk stable_id -> rekordbox row meta (folder/anlz/comment/genre).

    One chunked query against state.db (vendor mapping) + one against
    master.plain.db (djmdContent) instead of a per-row resolve. Missing
    state.db means no vendor mappings can exist (make_backend would be on
    InMemoryBackend) -- an empty result is the true state, not a fallback.
    A missing master.plain.db while mappings exist still fails loudly via
    :func:`apps.adapters.rekordbox.errors._open_ro`. Ids without a live mapping/content row are
    absent from the result (real states: non-rekordbox track, deleted row,
    tombstoned mapping). The vendor-mapping query filters `deleted_at IS
    NULL` to match `apps.adapters.rekordbox.paths.resolve_content`'s own
    predicate (#736 review) -- a tombstoned track_vendor_ids row can outlive
    the djmdContent row it once pointed at, and this must call that unmapped
    the same way the write path does, or has_rb_mapping reports True for a
    track resolve_content will refuse. ``state_db_path`` names the app's own
    state database when it serves one other than the process default.
    """
    ids = list(dict.fromkeys(stable_ids))
    state_path = state_db_path if state_db_path is not None else config.STATE_DB
    if not ids or not state_path.exists():
        return {}
    state = _open_ro(state_path, "STATE_DB")
    try:
        vendor_by_sid: dict[str, str] = {}
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            for sid, vid in state.execute(
                "SELECT stable_id, vendor_id FROM track_vendor_ids "
                "WHERE vendor = 'rekordbox' AND deleted_at IS NULL "
                f"AND stable_id IN ({placeholders})",
                tuple(chunk),
            ):
                vendor_by_sid[str(sid)] = str(vid)
    finally:
        state.close()
    if not vendor_by_sid:
        return {}

    master = _open_ro(config.MASTER_PLAIN_DB, "MASTER_DB")
    try:
        content_by_vid: dict[str, tuple[Any, ...]] = {}
        vendor_ids = sorted(set(vendor_by_sid.values()))
        for chunk in _chunked(vendor_ids):
            placeholders = ",".join("?" * len(chunk))
            for vid, folder, image, adp, comment, genre, play_count in master.execute(
                "SELECT c.ID, c.FolderPath, c.ImagePath, c.AnalysisDataPath, c.Commnt, "
                "       g.Name, c.DJPlayCount "
                "FROM djmdContent c "
                "LEFT JOIN djmdGenre g "
                "       ON g.ID = c.GenreID AND g.rb_local_deleted = 0 "
                # Unary + keeps rb_local_deleted off the index: rekordbox indexes
                # it, and without stats sqlite picks that index over the ID key,
                # walking every live row per page instead of seeking the ids.
                f"WHERE c.ID IN ({placeholders}) AND +c.rb_local_deleted = 0",
                tuple(chunk),
            ):
                content_by_vid[str(vid)] = (folder, image, adp, comment, genre, play_count)
    finally:
        master.close()

    out: dict[str, RbRowMeta] = {}
    for sid, vid in vendor_by_sid.items():
        content = content_by_vid.get(vid)
        if content is None:
            continue
        folder, image, adp, comment, genre, play_count = content
        out[sid] = RbRowMeta(
            vendor_id=vid,
            folder_path=folder or None,
            analysis_data_path=adp or None,
            comment=comment or None,
            genre=genre or None,
            play_count=int(play_count or 0),
            image_path=image or None,
        )
    return out


def _folder_by_sid(
    stable_ids: Sequence[str],
    state_file_paths: Mapping[str, str | None],
    metas: Mapping[str, RbRowMeta],
) -> dict[str, str | None]:
    """Rekordbox FolderPath when mapped, else the state-layer file_path."""
    return {
        sid: metas[sid].folder_path if metas.get(sid) is not None else state_file_paths.get(sid)
        for sid in stable_ids
    }


def bulk_availability_status(
    stable_ids: Sequence[str],
    state_file_paths: Mapping[str, str | None],
    metas: Mapping[str, RbRowMeta] | None = None,
    *,
    probe_mode: AvailabilityProbeMode = AvailabilityProbeMode.ROW_HYDRATION,
    folder_probed: Mapping[str, PathProbeResult] | None = None,
    budget: ProbeBudget | None = None,
) -> dict[str, FileAvailabilityStatus]:
    """``file_availability`` per stable_id: the primary path (FolderPath when
    mapped, else the state-layer file_path) and every ``track_locations``
    alternate, all under ONE probe budget. ``folder_probed`` lets a caller
    reuse primary results it already paid for; pass the same ``budget``."""
    if metas is None:
        metas = bulk_rb_meta(stable_ids)
    folder_by_sid = _folder_by_sid(stable_ids, state_file_paths, metas)
    budget = budget if budget is not None else ProbeBudget(probe_mode)
    if folder_probed is None:
        folder_probed = bulk_probe_paths(local_paths(folder_by_sid.values()), budget=budget)
    return classify_rows(folder_by_sid, folder_probed, budget)


def bulk_availability(
    stable_ids: Sequence[str],
    state_file_paths: Mapping[str, str | None],
    metas: Mapping[str, RbRowMeta] | None = None,
) -> dict[str, bool]:
    """``file_exists`` per stable_id as a plain bool.

    A bool cannot say "pending", so this is a FULL scan (see the
    availability module docstring): True only when a copy is present.
    """
    statuses = bulk_availability_status(
        stable_ids,
        state_file_paths,
        metas,
        probe_mode=AvailabilityProbeMode.FULL_SCAN,
    )
    return {sid: status == "present" for sid, status in statuses.items()}


def bulk_availability_for_playlist_summary(
    stable_ids: Sequence[str],
    state_file_paths: Mapping[str, str | None],
) -> dict[str, FileAvailabilityStatus]:
    """Index-backed availability for playlist tree ``available_count``:
    zero in-request stats, stale answers refreshed in the background."""
    return bulk_availability_status(
        stable_ids,
        state_file_paths,
        probe_mode=AvailabilityProbeMode.TREE_SUMMARY,
    )


def _quality_for(
    path: str | None,
    result: PathProbeResult | None,
    duration_ms: int | None,
) -> dict:
    if not path:
        return audio_quality.classify(None, None).as_dict()
    if is_streaming_path(path):
        # Streaming rows (Tidal/SoundCloud/Spotify) have no local file.
        return audio_quality.Quality(
            None, None, "", False, "streaming track, no local file"
        ).as_dict()
    suffix = Path(path).suffix.lower()
    if result is None or result.status == PENDING:
        return audio_quality.Quality(None, None, suffix, False, "availability pending").as_dict()
    if result.materialised_size is None:
        return audio_quality.Quality(None, None, suffix, False, "file missing").as_dict()
    return audio_quality.classify(path, duration_ms, result.materialised_size).as_dict()


def bulk_quality(
    stable_ids: Sequence[str],
    folder_by_sid: Mapping[str, str | None],
    duration_ms_by_sid: Mapping[str, int | None],
    *,
    probe_mode: AvailabilityProbeMode = AvailabilityProbeMode.ROW_HYDRATION,
    probed: Mapping[str, PathProbeResult] | None = None,
) -> dict[str, dict]:
    """Venue-rung quality dict per stable_id (apps.shared.audio_quality).

    Sizes come from the SAME budgeted probe that answers file_availability
    (pass ``probed`` to reuse it) -- so a 1000-row listing pays no extra
    stat for the badge. Streaming URIs, missing files and pending paths get
    an honest UNKNOWN rather than a guessed rung.
    """
    if probed is None:
        probed = bulk_probe_paths(local_paths(folder_by_sid.values()), probe_mode=probe_mode)
    return {
        sid: _quality_for(
            folder_by_sid.get(sid), probed.get(folder_by_sid.get(sid) or ""),
            duration_ms_by_sid.get(sid),
        )
        for sid in stable_ids
    }


def _state_text(track: Any, field_name: str) -> str | None:
    """Return a string-valued import-time state field without coercion."""
    field = track.provenance.get(field_name)
    return field.value if field is not None and isinstance(field.value, str) else None


def _editable_text(track: Any, field_name: str, vendor_value: str | None) -> str | None:
    field = track.provenance.get(field_name)
    if field is not None and field.source == "webui" and isinstance(field.value, str):
        return field.value
    if vendor_value:
        return vendor_value
    return _state_text(track, field_name)


def _genre_for_display(
    track: Any, meta: RbRowMeta | None
) -> tuple[str | None, str | None]:
    """Return (genre, genre_reason). A real genre suppresses any reason."""
    genre = _editable_text(track, "genre", meta.genre if meta is not None else None)
    if genre:
        return genre, None
    if meta is not None:
        return None, GENRE_REASON_NO_REKORDBOX_GENRE
    if not HAS_TAG_READER:
        return None, GENRE_REASON_TAG_READER_MISSING
    return None, GENRE_REASON_NO_FILE_TAG


_BPM_SOURCE_LABELS: dict[str, str] = {
    "rekordbox": "Rekordbox beatgrid",
    "own_beatgrid": "Own beatgrid analysis",
    "mik": "Mixed In Key",
    "energy_peak": "Energy peak detection",
    "librosa": "Librosa tempo",
    "essentia": "Essentia rhythm",
}


def _bpm_analytics_wire(track: Any) -> dict[str, Any]:
    """Beatgrid/BPM lane provenance for library hover (LIBUX-21)."""
    field = track.provenance.get("bpm")
    if field is None:
        return {}
    out: dict[str, Any] = {}
    src = getattr(field, "source", None)
    if isinstance(src, str) and src:
        out["bpm_source"] = src
        out["bpm_method"] = _BPM_SOURCE_LABELS.get(src, src.replace("_", " "))
    conf = getattr(field, "confidence", None)
    if conf is not None:
        c = _valid_bpm_confidence(conf)
        if c is not None:
            out["bpm_confidence"] = c
        else:
            # Fail loud but scoped to this row (PR #4014, Sol P1): raising
            # here would 500 the whole library listing over one bad stored
            # value, while dropping it silently reads as "no confidence".
            out["bpm_confidence_error"] = (
                f"invalid stored bpm confidence {conf!r}: expected a number from 0 to 1"
            )
    return out


def _valid_bpm_confidence(conf: Any) -> float | None:
    """Return the confidence as a float in [0, 1], or None when it is invalid."""
    if isinstance(conf, bool):
        return None
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return None
    return c if math.isfinite(c) and 0.0 <= c <= 1.0 else None


def _scalar_lane_display(
    track: Any,
    field_name: str,
) -> tuple[str | None, str, str | None]:
    """Return (cell value, status, reason) for a projected scalar lane."""
    field = track.provenance.get(field_name)
    if field is None:
        return None, "missing", f"{field_name} not analyzed"
    if field.status == "failed":
        reason = (
            field.reason
            if isinstance(field.reason, str) and field.reason
            else f"{field_name} analysis failed"
        )
        return None, "failed", reason
    if field.status == "missing":
        reason = (
            field.reason
            if isinstance(field.reason, str) and field.reason
            else f"{field_name} not analyzed yet"
        )
        return None, "missing", reason
    if field.status == "available-not-selected":
        reason = (
            field.reason
            if isinstance(field.reason, str) and field.reason
            else f"{field_name} available but not selected"
        )
        return field.value, "available-not-selected", reason
    value = field.value
    if field_name == "key" and value is not None and not isinstance(value, str):
        value = str(value)
    return value, "ok", None


def _energy_for_display(track: Any) -> tuple[int | None, str | None, str]:
    """Return the narrow-column value and its honest absence explanation.

    The browser's display scale is deliberately 1-9. Only a value imported
    from Mixed In Key is eligible: a computed/inferred value must never look
    like MIK data, and a MIK 10 must not be silently squeezed into nine.
    """
    field = track.provenance.get("energy")
    if field is None:
        return None, None, "no Mixed In Key energy has been imported"
    if field.source != "mik":
        return None, None, f"source is {field.source}, not Mixed In Key"
    value = field.value
    energy = readable_mik_energy(value)
    if energy is None:
        is_whole_number = isinstance(value, (int, float)) and float(value).is_integer()
        if isinstance(value, bool) or not is_whole_number:
            return None, None, "Mixed In Key energy is not a whole number"
        return None, None, f"Mixed In Key value {int(value)} is outside the 1-9 display scale"
    return energy, "mik", "Mixed In Key energy, 1-9 display scale"


_REMIX_MARKER_RE = re.compile(
    r"\b(?:remix|bootleg|rework|refix|re-edit|flip|mashup|mash-up|vip)\b",
    re.IGNORECASE,
)
_EDIT_RE = re.compile(r"\bedit\b", re.IGNORECASE)
_RADIO_EDIT_RE = re.compile(r"\bradio\s+edit\b", re.IGNORECASE)


def is_remix_title(title: str | None) -> bool:
    """Title-marker remix heuristic for the browser 'Remixes' filter."""
    if title is None:
        return False
    if _REMIX_MARKER_RE.search(title) is not None:
        return True
    return _EDIT_RE.search(_RADIO_EDIT_RE.sub("", title)) is not None


def is_radio_edit_title(title: str | None) -> bool:
    """Radio edits are length trims, deliberately NOT remixes."""
    return title is not None and _RADIO_EDIT_RE.search(title) is not None


def _lyrics_summary(v: lyric_store.LyricVerdict) -> dict[str, Any]:
    return {
        "verdict": v.verdict,
        "effective": v.effective,
        "n_words": v.n_words,
        "n_lines": v.n_lines,
        "has_words": v.words_content_hash is not None,
        "pct_witness_red": v.pct_witness_red,
        "source": v.source,
        "language_iso3": v.language_iso3,
        "override": v.override,
    }


def _cloud_transfer_read_model(
    stable_ids: Sequence[str],
    *,
    jobs_store: JobStore | None = None,
) -> dict[str, transfer_status.CloudTransfer]:
    """Union durable locations with ephemeral transfer + active hydrate jobs."""
    result = transfer_status.transfers_for(stable_ids)
    if jobs_store is None:
        return result
    wanted = set(stable_ids)
    for job in jobs_store.list(limit=500):
        if job.get("kind") != "cloud.hydrate":
            continue
        if job.get("status") not in ("queued", "running"):
            continue
        payload = job.get("payload") or {}
        stable_id = payload.get("stable_id")
        if not isinstance(stable_id, str) or stable_id not in wanted:
            continue
        if stable_id in result:
            continue
        progress = job.get("progress")
        bytes_transferred = 0
        if isinstance(progress, (int, float)) and progress > 0:
            bytes_transferred = max(1, int(float(progress) * 100))
        result[stable_id] = transfer_status.CloudTransfer(
            direction="download",
            bytes_transferred=bytes_transferred,
            bytes_total=100 if bytes_transferred else None,
        )
    return result


def build_track_rows(
    tracks: Sequence[Any],
    *,
    jobs_store: JobStore | None = None,
    probe_mode: AvailabilityProbeMode = AvailabilityProbeMode.ROW_HYDRATION,
    data_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Hydrated track rows (contract item 4) for playlist detail + listings.

    ``tracks`` are backend ``Track`` dataclasses in the order to render
    (playlist membership order / page order). One bulk vendor lookup, one
    cached stat pass and per-row cached preview extraction replace the
    old 29x per-row GET fan-out. Field names match the shared API
    contract exactly: title, artist, key, bpm, rating, duration_ms,
    genre, comments, etag, preview_b64, preview_max, file_exists,
    is_streaming, is_remote, has_remote_copy, cloud_transfer, spotify_pending,
    quality, play_count, vocals, stems,
    artwork_available, artwork_status.

    A row's preview strip, vocals lookup and artwork check can resolve the
    same ``AnalysisDataPath`` independently, with repeated paths across rows.
    One :class:`~apps.shared.platform_paths.AssetResolver` is created here
    and threaded through every per-row lookup below, so each distinct asset
    path resolves once for this whole call and is discarded when it returns
    -- see that class's docstring for why a persistent, time-based cache was
    rejected on review instead.
    """
    resolver = AssetResolver()
    stable_ids = [t.stable_id for t in tracks]
    # JEV genre guesses (GENRE-02): a sidecar beside state.db, cached by mtime.
    # JEV guesses come from the caller's library: every route passes the app's
    # configured data root (deps.get_library_data_dir). Direct callers with no
    # app fall back to the process-global one.
    jev_doc = jev_store.load_suggestions(data_dir if data_dir is not None else config.STATE_DB.parent.parent)
    cloud_transfers = _cloud_transfer_read_model(
        stable_ids, jobs_store=jobs_store
    )
    metas = bulk_rb_meta(stable_ids)
    state_file_paths = {t.stable_id: t.file_path for t in tracks}
    folder_by_sid = _folder_by_sid(stable_ids, state_file_paths, metas)
    # ONE budget for the whole request: primaries first, then the
    # track_locations alternates of rows whose primary is not present.
    budget = ProbeBudget(probe_mode)
    folder_probed = bulk_probe_paths(local_paths(folder_by_sid.values()), budget=budget)
    availability = bulk_availability_status(
        stable_ids,
        state_file_paths,
        metas,
        folder_probed=folder_probed,
        budget=budget,
    )
    quality = bulk_quality(
        stable_ids,
        folder_by_sid,
        {t.stable_id: t.duration_ms for t in tracks},
        probed=folder_probed,
    )
    stems = bulk_stem_summaries(stable_ids)
    remote_sids: set[str] = set()
    lyric_verdicts: dict[str, lyric_store.LyricVerdict] = {}
    # Rows with no rekordbox mapping take their artwork verdict from the
    # state layer. One connection answers all of them (issue #3962); with no
    # state.db nothing is materialised here, the per-row function's False.
    unmapped_ids = list(dict.fromkeys(sid for sid in stable_ids if sid not in metas))
    local_artwork: dict[str, bool | None] = dict.fromkeys(unmapped_ids, False)
    # PLAYS-01: Open DJ's own plays, added to rekordbox's DJPlayCount below.
    own_plays: dict[str, play_log.OwnPlays] = {}
    if config.STATE_DB.exists():
        state = _open_ro(config.STATE_DB, "STATE_DB")
        try:
            remote_sids = remote_status.sids_with_remote_copy(state, stable_ids)
            lyric_verdicts = lyric_store.bulk_verdicts(state, stable_ids)
            local_artwork = bulk_local_artwork_available(state, unmapped_ids)
            own_plays = play_log.bulk_own_plays(state, stable_ids)
        finally:
            state.close()
    mounted = remote_status.mounted_volumes()
    rows: list[dict[str, Any]] = []
    for track in tracks:
        meta = metas.get(track.stable_id)
        folder = meta.folder_path if meta is not None else track.file_path
        preview_b64, preview_max = row_preview_strip(
            track.stable_id, meta, resolver=resolver
        )
        artwork_available, artwork_status = (
            _local_artwork_facts(local_artwork[track.stable_id])
            if meta is None
            else _rb_artwork_facts(meta, resolver=resolver)
        )
        energy, energy_source, energy_reason = _energy_for_display(track)
        genre, genre_reason = _genre_for_display(track, meta)
        key_value, key_status, key_reason = _scalar_lane_display(track, "key")
        if key_status == "missing" and track.key is not None:
            key_value = track.key
            key_status = "ok"
            key_reason = None
        bpm_value, bpm_status, bpm_reason = _scalar_lane_display(track, "bpm")
        if bpm_status == "missing" and track.bpm is not None:
            bpm_value = track.bpm
            bpm_status = "ok"
            bpm_reason = None
        _, loudness_status, loudness_reason = _scalar_lane_display(track, "loudness_lufs")
        content = RbContent(
            stable_id=track.stable_id,
            vendor_id=meta.vendor_id if meta is not None else "",
            folder_path=folder,
            image_path=None,
            analysis_data_path=(meta.analysis_data_path if meta is not None else None),
            length_s=None,
            comment=None,
            genre=None,
        )
        file_availability = availability[track.stable_id]
        file_exists = status_to_file_exists(file_availability)
        streaming = is_streaming_row(folder, file_exists=file_exists is True)
        has_remote_copy = track.stable_id in remote_sids
        cloud_transfer = cloud_transfers.get(track.stable_id)
        rows.append(
            {
                "stable_id": track.stable_id,
                # True exactly when this track has rekordbox meta to serve:
                # bulk_rb_meta yields a row only for a stable_id that has BOTH
                # a rekordbox track_vendor_ids mapping AND a live djmdContent
                # row, which are the same two conditions resolve_content
                # checks before it raises VENDOR_MAPPING_NOT_FOUND. False is a
                # real library state (locally imported track, djay-only
                # mapping, deleted rekordbox row). Since #505 rb-meta serves
                # those a 200 local-vendor payload, so this flag no longer
                # predicts a 404 -- it tells the browser the payload would
                # carry nothing the listing row does not already have, so the
                # per-row fetch can be skipped entirely.
                "has_rb_mapping": meta is not None,
                "artwork_available": artwork_available,
                "artwork_status": artwork_status,
                "title": track.title,
                "artist": track.artist,
                "key": (
                    key_value
                    if key_value is not None
                    else track.key
                    if key_status == "ok"
                    else None
                ),
                "key_status": key_status,
                "key_reason": key_reason,
                "loudness_status": loudness_status,
                "loudness_reason": loudness_reason,
                "bpm": (
                    bpm_value
                    if bpm_value is not None
                    else track.bpm
                    if bpm_status == "ok"
                    else None
                ),
                "bpm_status": bpm_status,
                "bpm_reason": bpm_reason,
                **_bpm_analytics_wire(track),
                "rating": track.rating,
                "energy": energy,
                "energy_source": energy_source,
                "energy_reason": energy_reason,
                "duration_ms": track.duration_ms,
                "genre": genre,
                "genre_reason": genre_reason,
                # A guess is served only while the track has no genre tag.
                "genre_guess": (
                    None
                    if (genre or "").strip()
                    else jev_store.genre_guess(jev_doc, track.stable_id)
                ),
                "comments": _editable_text(
                    track, "comments", meta.comment if meta is not None else None
                ),
                "etag": compute_etag(track.stable_id, track.updated_at, track.selection_tag),
                "preview_b64": preview_b64,
                "preview_max": preview_max,
                "file_availability": file_availability,
                "file_exists": file_exists,
                "is_streaming": streaming,
                "is_remote": remote_status.is_remote_audio(
                    is_streaming=streaming,
                    is_awaiting_volume=remote_status.is_awaiting_volume(folder, mounted=mounted),
                    has_local_audio=file_exists is True,
                    has_remote_copy=has_remote_copy,
                ),
                "has_remote_copy": has_remote_copy,
                "cloud_transfer": (
                    {
                        "direction": cloud_transfer.direction,
                        "bytes_transferred": cloud_transfer.bytes_transferred,
                        "bytes_total": cloud_transfer.bytes_total,
                    }
                    if cloud_transfer is not None
                    else None
                ),
                # Synthetic unmatched Spotify rows (spotify-pending:{sha1}).
                "spotify_pending": str(track.stable_id).startswith("spotify-pending:"),
                "quality": quality[track.stable_id],
                "play_count": (meta.play_count if meta is not None else 0)
                + (own_plays[track.stable_id].count if track.stable_id in own_plays else 0),
                "vocals": vocals_for_content(content, resolver=resolver),
                "stems": stems[track.stable_id],
                "lyrics": (
                    _lyrics_summary(lyric_verdicts[track.stable_id])
                    if track.stable_id in lyric_verdicts
                    else None
                ),
                "is_remix": is_remix_title(track.title),
                "is_radio_edit": is_radio_edit_title(track.title),
            }
        )
    return rows


def row_preview_strip(
    stable_id: str,
    meta: RbRowMeta | None,
    *,
    resolver: AssetResolver | None = None,
) -> tuple[str | None, int | None]:
    """One listing row's ``(preview_b64, preview_max)`` strip.

    A rekordbox-mapped row reads its strip out of the ANLZ files first. When
    there is no mapping (issue #735), or the mapping carries no ANLZ preview
    (issue #4510: the /anlz route decodes such a row locally), the row falls
    back to the strip WE decoded, so a row whose only waveform is the local
    decode renders it in the listing instead of a dash until it is selected.
    Cache-read-only on purpose: hydrating 200 rows must never fan out 200
    ffmpeg processes, so the decode stays on the single-track /anlz path and
    a row before that decode shows the honest dash. Neither source yields
    ``(None, None)``, never an invented strip.
    """
    if meta is not None:
        rb_strip = preview_strip(meta.analysis_data_path, resolver=resolver)
        if rb_strip[0] is not None:
            return rb_strip
    return local_preview_strip(stable_id)


def _artwork_facts(
    meta: RbRowMeta | None,
    stable_id: str,
    *,
    resolver: AssetResolver | None = None,
) -> tuple[bool | None, str]:
    """Return the same artwork verdict the artwork route would serve.

    The listing has already bulk-resolved each record, so deciding this here
    removes the fragile visible-row /rb-meta request gate entirely.

    ``resolver``: see :func:`build_track_rows` (pin ad59ac) -- the same
    per-call memo threaded through every asset-path lookup for one call.
    """
    if meta is None:
        return _local_artwork_facts(local_artwork_available(stable_id))
    return _rb_artwork_facts(meta, resolver=resolver)


def _local_artwork_facts(available: bool | None) -> tuple[bool | None, str]:
    """Wire pair for a row with no rekordbox mapping (tri-state, see paths)."""
    if available is True:
        return True, "ok"
    if available is False:
        return False, "no_image_path"
    return None, "unresolved"


def _rb_artwork_facts(
    meta: RbRowMeta, *, resolver: AssetResolver | None = None,
) -> tuple[bool | None, str]:
    """Wire pair for a rekordbox-mapped row: its pre-rendered small cover."""
    if meta.image_path is None:
        return False, "no_image_path"
    mapped = resolve_asset_path(meta.image_path, resolver=resolver)
    if mapped.resolved is None:
        return False, "unresolved"
    artwork = mapped.resolved.parent / config.ARTWORK_FILENAMES["s"]
    if not artwork.is_file():
        return False, "file_missing"
    return True, "ok"


__all__ = [
    "GENRE_REASON_NO_FILE_TAG",
    "GENRE_REASON_NO_REKORDBOX_GENRE",
    "GENRE_REASON_TAG_READER_MISSING",
    "PROBE_BUDGET_ROW_HYDRATION",
    "PROBE_BUDGET_TREE_SUMMARY",
    "AvailabilityProbeMode",
    "FileAvailabilityStatus",
    "PathProbeResult",
    "ProbeBudget",
    "build_track_rows",
    "bulk_availability",
    "bulk_availability_for_playlist_summary",
    "bulk_availability_status",
    "bulk_file_exists",
    "bulk_file_size",
    "bulk_probe_paths",
    "bulk_quality",
    "bulk_rb_meta",
    "is_radio_edit_title",
    "is_remix_title",
]
