"""HTTP trigger + status for vocal analysis -- issue #1038 / PARITY-08.

Vocal analysis (``apps.vocals``: scan, trickle, one, from-stems) was
CLI-only, so an agent driving the webui had no way to request it and the
UI had no control to surface it -- the agent-native parity hole this
router closes.

Deliberately a thin facade, no new job store:

  * ``POST /vocals/analyze`` classifies every requested track with the
    SAME categories :func:`apps.vocals.cli.classify` already assigns
    (``missing_analysis``, ``missing_file``, ``pvdi``, ``cached_demucs``)
    and only enqueues a track in the ``todo`` category, through the
    existing claim/lease path (``mode: "one"``, real demucs) or the
    existing stems-derive path (``mode: "from-stems"``, CPU only). A
    track outside ``todo`` is refused with that same category, not
    silently skipped.
  * A track rekordbox does not hold (a folder import, or any track on an
    install with no decrypted rekordbox copy) is keyed by the app's own
    stable id and needs only its audio file: it is never refused for a
    missing rekordbox mapping or ANLZ file, and a missing
    ``master.plain.db`` is never an error on status. ``POST
    /vocals/analyze`` decides once up front: a copy that exists but cannot
    be read (a directory, a dangling link, an empty or corrupt file) is a
    broken install and answers 503 ``MASTER_DB_UNREADABLE``, because falling
    back would skip the ANLZ and PVDI checks of every rekordbox-held track.
    ``GET /vocals/{stable_id}/status`` probes the copy only when that track
    has a rekordbox mapping; a folder-only track is served from the
    vocal-cache even when the copy is broken.
  * ``GET /vocals/{stable_id}/status`` reuses the exact vocal-cache
    reader ``/anlz`` already serves through
    ``rb_vendor.vocals_for_content`` (``rb_assets.py``'s ``vocals`` field),
    so the two never disagree about what "analyzed" means for a track.

Requirements (mini-PRD):
  ✔︎ ✅ POST /vocals/analyze: enqueue by stable_ids or playlist_id.
    [if] a track has a stem bundle and mode is from-stems [then] a
    vocal-cache entry appears and it is reported claimed
    [if] a track has no local ANLZ .DAT [then ⛔️] refused missing_analysis,
    nothing enqueued
    [if] neither stable_ids nor playlist_id is given, or both are [then ⛔️] 422
    [if] there is no master.plain.db [then] 202 with a per-track answer,
    never a 500
    [if] master.plain.db exists but is not a readable database [then ⛔️]
    503 MASTER_DB_UNREADABLE, nothing claimed
    [if] a folder-imported track has a stem bundle [then] claimed and cached
    [if] a folder-imported track has no stem bundle [then ⛔️] refused
    missing_stems (it needs stems first)
  ✔︎ ✅ GET /vocals/{stable_id}/status: the same four vocals states /anlz serves.
    [if] a folder-imported track has a valid vocal-cache entry [then] demucs
    [if] the last attempt failed after the 202 [then] not_analyzed carries
    failed_attempt with the reason, until the next attempt starts
"""
from __future__ import annotations

import logging
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.errors import unavailable
from apps.shared.platform_paths import load_path_map
from apps.shared.state import locations as state_locations
from apps.stems.artifacts import StemArtifactError, StemBundleNotFoundError
from apps.vocals import attempts as vattempts
from apps.vocals import cache as vcache
from apps.vocals import from_stems as vfrom_stems
from apps.vocals.cli import (
    CATEGORY_BROKEN_CACHE,
    CATEGORY_CACHED,
    CATEGORY_MISSING,
    CATEGORY_STEM_DERIVABLE,
    CATEGORY_TODO,
    WORKER_TIMEOUT_S,
    Ctx,
    TrackClaim,
    VocalTrack,
    _claim_track,
    _managed_track_claim,
    _open_ro,
    _process_one,
    classify,
    load_state_tracks,
    master_content_rows,
    rekordbox_mappings,
    tracks_from_rows,
)

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import (
    get_library_data_dir,
    get_library_state_db,
    get_read_state,
    get_write_state,
)
from ..vocal_anchor_validation import validate_vocal_anchor_for_vocals

router = APIRouter(prefix="/vocals", tags=["vocals"])

# Refusal categories not already covered by apps.vocals.cli's CATEGORY_*
# constants: those classify a track by its EXISTING analysis state, these
# by what THIS request could not do with it.
CATEGORY_UNKNOWN_TRACK: str = "unknown_track"
CATEGORY_MISSING_STEMS: str = "missing_stems"
CATEGORY_IN_PROGRESS: str = "in_progress"

log = logging.getLogger(__name__)


class VocalsAnalyzeIn(BaseModel):
    stable_ids: list[str] | None = Field(default=None, min_length=1)
    playlist_id: str | None = None
    mode: Literal["from-stems", "one"]

    @model_validator(mode="after")
    def _exactly_one_source(self) -> VocalsAnalyzeIn:
        if (self.stable_ids is None) == (self.playlist_id is None):
            raise ValueError("exactly one of stable_ids or playlist_id is required")
        return self


class VocalsAnalyzeOut(BaseModel):
    claimed: list[str]
    refused: dict[str, str]


class VocalsRegionOut(BaseModel):
    start_s: float
    end_s: float
    intensity: int
    confidence: float | None = None


class VocalsFailedAttemptOut(BaseModel):
    """Why the last requested analysis of a track produced nothing."""

    reason: str
    error_type: str
    failed_at: str | None


class VocalsStatusOut(BaseModel):
    """The four states of ``/anlz``'s ``vocals`` field, plus the reason a
    ``not_analyzed`` track's last requested analysis failed, when it did."""

    stable_id: str
    status: Literal["rekordbox", "no_vocals", "demucs", "not_analyzed"]
    fps: float | None = None
    regions: list[VocalsRegionOut] = Field(default_factory=list)
    failed_attempt: VocalsFailedAttemptOut | None = None


class VocalCorrectionValidateIn(BaseModel):
    anchor: str = Field(min_length=1)


class VocalCorrectionValidateOut(BaseModel):
    stable_id: str
    anchor: str
    kind: Literal["region", "at"]
    status: Literal["rekordbox", "no_vocals", "demucs", "not_analyzed"]
    start_s: float | None = None
    end_s: float | None = None
    intensity: int | None = None
    time_s: float | None = None


def _resolve_stable_ids(body: VocalsAnalyzeIn, backend: StateBackend) -> list[str]:
    if body.stable_ids is not None:
        return list(body.stable_ids)
    assert body.playlist_id is not None  # enforced by _exactly_one_source
    playlist = backend.get_playlist(body.playlist_id)  # 404s via NotFoundError
    return list(playlist.items)


def _unique_stable_ids(stable_ids: list[str]) -> list[str]:
    """Preserve request order while preventing duplicate worker execution."""
    seen: set[str] = set()
    unique: list[str] = []
    for stable_id in stable_ids:
        if stable_id in seen:
            continue
        seen.add(stable_id)
        unique.append(stable_id)
    return unique


def _request_vocals_ctx(request: Request) -> Ctx:
    """The exact state database and derived cache root selected for this app."""
    return Ctx(
        data_dir=get_library_data_dir(request),
        state_db_path=get_library_state_db(request),
    )


def _master_db_unreadable(path: Path, why: str) -> HTTPException:
    return unavailable(
        "MASTER_DB_UNREADABLE",
        f"the rekordbox copy at {path} exists but cannot be read ({why}); "
        "refusing to treat this library as having no rekordbox",
    )


def _vocal_cache_entry_unusable(path: Path, why: str) -> ValueError:
    """Same shape ``apps.vocals.cache`` raises for a corrupt entry: a
    ``ValueError`` naming the entry. A broken cache is a bug, not a miss."""
    return ValueError(f"vocal-cache entry {path} is not a readable regular file ({why})")


def _regular_file_present(path: Path, broken: Callable[[Path, str], Exception]) -> bool:
    """THE absence probe of this module, one ``lstat``.

    False only for ``FileNotFoundError``: nothing is at ``path``. True only
    for a regular file (through links). Everything else is something there
    that is broken, and raises ``broken(path, why)`` rather than read as
    absent: a directory, a dangling link, a parent the engine may not enter
    (``PermissionError``), and a parent that is itself a file
    (``NotADirectoryError``: the data dir is damaged, which says nothing
    about what the user has).

    The pathlib type predicates are not used on purpose: they swallow a
    different set of ``OSError`` on each Python version, so the same locked
    directory is a bare 500 on one and "absent" on another.
    """
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise broken(path, f"{type(exc).__name__}: {exc}") from exc
    if stat.S_ISLNK(mode):
        try:
            mode = path.stat().st_mode
        except OSError as exc:  # dangling, looping, or into a place we may not enter
            raise broken(path, f"link target unusable, {type(exc).__name__}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise broken(path, "not a regular file")
    return True


def _rekordbox_copy_present(path: Path) -> bool:
    """Does this machine have a rekordbox copy: absent is a normal library
    with no rekordbox, broken is the typed 503."""
    return _regular_file_present(path, _master_db_unreadable)


def _status_mapping_exists(stable_id: str) -> bool:
    """Whether state.db maps this stable id to a live rekordbox vendor row."""
    state = _open_ro(rb_config.STATE_DB, "STATE_DB")
    try:
        row = state.execute(
            "SELECT 1 FROM track_vendor_ids "
            "WHERE stable_id = ? AND vendor = 'rekordbox' AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
        return row is not None
    finally:
        state.close()


def _vocal_cache_entry_present(data_dir: Path, stable_id: str) -> bool:
    """Is there a vocal-cache entry: absent is "not analyzed yet", broken raises.

    Probes the entry under its OWN name. ``vcache.cache_path`` follows links,
    so a dangling link would come back as its missing target and read as
    absent; it is still called first, for its confinement check.
    """
    own_name = vcache.cache_dir(data_dir) / f"{stable_id}.json"
    try:
        vcache.cache_path(data_dir, stable_id)
    except OSError as exc:
        raise _vocal_cache_entry_unusable(own_name, f"{type(exc).__name__}: {exc}") from exc
    return _regular_file_present(own_name, _vocal_cache_entry_unusable)


def _rekordbox_tracks(ctx: Ctx) -> dict[str, VocalTrack]:
    """Tracks rekordbox holds, by stable id: none on an install with no
    decrypted rekordbox copy, which is a normal library and not an error.

    "No rekordbox" is decided ONCE, by the probe on the first line. Once it
    has seen a copy, every later failure to read it in this request is the
    typed 503, including the copy vanishing before the open: reclassifying
    to absent there would send every rekordbox-held track down the stable-id
    path past its ANLZ and PVDI checks.
    """
    if not _rekordbox_copy_present(ctx.master_db):
        return {}
    # state.db work stays OUTSIDE the translation below: its failure keeps its
    # own error, and is never reported under the rekordbox copy's name.
    by_vendor = rekordbox_mappings(ctx, None)
    try:
        # Only operations on ctx.master_db in here. Read it for real first:
        # no row is fetched when state.db maps no track to rekordbox, so an
        # empty or corrupt file would otherwise pass as a healthy copy that
        # happens to hold nothing.
        master = _open_ro(ctx.master_db, "MASTER_DB")
        try:
            master.execute("SELECT 1 FROM djmdContent LIMIT 1").fetchone()
        finally:
            master.close()
        rows = master_content_rows(ctx, list(by_vendor))
    except (OSError, sqlite3.Error) as exc:
        raise _master_db_unreadable(ctx.master_db, f"{type(exc).__name__}: {exc}") from exc
    return {t.stable_id: t for t in tracks_from_rows(ctx, by_vendor, rows)}


def _classify_own(
    ctx: Ctx,
    tracks: list[VocalTrack],
    *,
    stem_roots: tuple[Path, ...] | None = None,
) -> None:
    """Categories for tracks rekordbox does not hold. No ANLZ or PVDI can
    exist for them, so only the audio file and the vocal-cache decide."""
    stem_candidates: list[VocalTrack] = []
    for tr in tracks:
        if not tr.audio_on_disk:
            tr.category = CATEGORY_MISSING
            continue
        try:
            cache_present = _vocal_cache_entry_present(ctx.data_dir, tr.stable_id)
            if (
                cache_present
                and vcache.load_valid_entry(
                    vcache.cache_path(ctx.data_dir, tr.stable_id), tr.audio_path
                )
                is not None
            ):
                tr.category = CATEGORY_CACHED
            else:
                stem_candidates.append(tr)
        except ValueError as exc:
            log.warning("broken vocal-cache for %s during classify: %s", tr.stable_id, exc)
            tr.category = CATEGORY_BROKEN_CACHE

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


def _own_vocals(ctx: Ctx, stable_id: str) -> dict[str, Any]:
    """The ``vocals`` field for a track rekordbox does not hold: its
    vocal-cache entry, valid against the audio the library plays, else
    ``not_analyzed``. A cache path that is there but broken raises."""
    if not _vocal_cache_entry_present(ctx.data_dir, stable_id):
        return {"status": "not_analyzed"}
    entry_path = vcache.cache_path(ctx.data_dir, stable_id)
    state = _open_ro(ctx.state_db, "STATE_DB")
    try:
        audio = state_locations.local_audio_path(
            state, stable_id, path_map=load_path_map(ctx.data_dir)
        )
    finally:
        state.close()
    if audio is None:
        return {"status": "not_analyzed"}
    entry = vcache.load_valid_entry(entry_path, audio)
    if entry is None:
        return {"status": "not_analyzed"}
    return vcache.anlz_vocals_of(entry)


def _own_vocals_for_serve(ctx: Ctx, stable_id: str) -> dict[str, Any]:
    """Like :func:`_own_vocals`, but a broken cache entry never escapes as a 500."""
    try:
        return _own_vocals(ctx, stable_id)
    except ValueError as exc:
        log.warning("broken vocal-cache for %s: %s", stable_id, exc)
        return {"status": "not_analyzed"}


def _process_claimed_track(ctx: Ctx, track: VocalTrack, claim: TrackClaim) -> None:
    """Run a claimed Demucs job after its HTTP response has been sent.

    Nobody is waiting on this call, so a failure is recorded for the status
    route before it is re-raised into the engine log. Both the clear of the
    previous failure (in ``_process_one``) and the write of this one happen
    while the claim is held, so no other attempt for the track can clear or
    run in between. A failure that cannot be recorded is noted on the error
    that caused it, which is still the one raised and the one the claim
    context sees.
    """
    with _managed_track_claim(claim):
        try:
            _process_one(ctx, track, "[http]", WORKER_TIMEOUT_S, claim)
        except Exception as exc:
            try:
                vattempts.record_failure(ctx.data_dir, track.stable_id, exc)
            except Exception as record_exc:  # noqa: BLE001  # noted on exc, which is re-raised
                exc.add_note(f"this failed attempt could not be recorded: {record_exc!r}")
            raise


@router.post("/analyze", response_model=VocalsAnalyzeOut, status_code=202)
def analyze_vocals(
    body: VocalsAnalyzeIn,
    request: Request,
    background_tasks: BackgroundTasks,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> VocalsAnalyzeOut:
    stable_ids = _unique_stable_ids(_resolve_stable_ids(body, backend))
    ctx = _request_vocals_ctx(request)
    rekordbox_by_id = _rekordbox_tracks(ctx)
    own_ids = [sid for sid in stable_ids if sid not in rekordbox_by_id]
    own_by_id = (
        {t.stable_id: t for t in load_state_tracks(ctx, own_ids)} if own_ids else {}
    )

    claimed: list[str] = []
    refused: dict[str, str] = {}
    candidates: list[VocalTrack] = []
    for sid in stable_ids:
        tr = rekordbox_by_id.get(sid) or own_by_id.get(sid)
        if tr is None:
            refused[sid] = CATEGORY_UNKNOWN_TRACK
            continue
        candidates.append(tr)

    configured_roots = getattr(request.app.state, "stem_roots", None)
    stem_roots = (
        tuple(Path(root) for root in configured_roots)
        if configured_roots is not None
        else None
    )

    classify(
        ctx,
        [tr for tr in candidates if tr.stable_id in rekordbox_by_id],
        stem_roots=stem_roots,
    )
    _classify_own(
        ctx,
        [tr for tr in candidates if tr.stable_id not in rekordbox_by_id],
        stem_roots=stem_roots,
    )
    for tr in candidates:
        if body.mode == "from-stems":
            if tr.category not in (CATEGORY_TODO, CATEGORY_STEM_DERIVABLE):
                refused[tr.stable_id] = tr.category
                continue
        elif tr.category != CATEGORY_TODO:
            refused[tr.stable_id] = tr.category
            continue
        assert tr.audio_path is not None  # todo / stem_derivable implies audio_on_disk

        if body.mode == "from-stems":
            roots = stem_roots
            try:
                vfrom_stems.write_from_bundle(
                    ctx.data_dir,
                    tr.stable_id,
                    tr.audio_path,
                    stem_roots=roots,
                )
            except (StemBundleNotFoundError, StemArtifactError):
                refused[tr.stable_id] = CATEGORY_MISSING_STEMS
                continue
            claimed.append(tr.stable_id)
        else:  # mode == "one"
            cache_file = vcache.cache_path(ctx.data_dir, tr.stable_id)
            claim = _claim_track(cache_file)
            if claim is None:
                refused[tr.stable_id] = CATEGORY_IN_PROGRESS
                continue
            background_tasks.add_task(_process_claimed_track, ctx, tr, claim)
            claimed.append(tr.stable_id)

    return VocalsAnalyzeOut(claimed=claimed, refused=refused)


def _resolve_vocals_dict(ctx: Ctx, stable_id: str) -> dict[str, Any]:
    """Shared vocals read model for status and vocal-area anchor validation."""
    try:
        content = rb_vendor.resolve_content(stable_id)
    except HTTPException as exc:
        detail: dict[str, str] = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code")
        if code == "VENDOR_MAPPING_NOT_FOUND":
            return _own_vocals_for_serve(ctx, stable_id)
        if code == "MASTER_DB_UNAVAILABLE":
            if not _rekordbox_copy_present(rb_config.MASTER_PLAIN_DB):
                return _own_vocals_for_serve(ctx, stable_id)
            raise
        raise
    except (OSError, sqlite3.Error) as exc:
        if _status_mapping_exists(stable_id):
            raise _master_db_unreadable(
                rb_config.MASTER_PLAIN_DB, f"{type(exc).__name__}: {exc}"
            ) from exc
        raise
    return rb_vendor.vocals_for_content(content)


@router.get("/{stable_id}/status", response_model=VocalsStatusOut)
def get_vocal_status(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> VocalsStatusOut:
    ctx = _request_vocals_ctx(request)
    vocals = _resolve_vocals_dict(ctx, stable_id)
    failed = (
        vattempts.load_failure(ctx.data_dir, stable_id)
        if vocals["status"] == "not_analyzed"
        else None
    )
    return VocalsStatusOut(
        stable_id=stable_id,
        status=vocals["status"],
        fps=vocals.get("fps"),
        regions=[VocalsRegionOut(**r) for r in vocals.get("regions", [])],
        failed_attempt=(
            VocalsFailedAttemptOut(
                reason=failed.reason,
                error_type=failed.error_type,
                failed_at=failed.failed_at,
            )
            if failed is not None
            else None
        ),
    )


@router.post(
    "/{stable_id}/validate-correction",
    response_model=VocalCorrectionValidateOut,
)
def validate_vocal_correction(
    stable_id: str,
    body: VocalCorrectionValidateIn,
    request: Request,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> VocalCorrectionValidateOut:
    """Validate a ``vocal-area:`` anchor against the live vocals model.

    ``POST /api/v1/feedback/comments`` remains the only comment writer (FB-03);
    this route is the agent-native validation twin (issue #13173).
    """
    ctx = _request_vocals_ctx(request)
    vocals = _resolve_vocals_dict(ctx, stable_id)
    payload = validate_vocal_anchor_for_vocals(stable_id, body.anchor, vocals)
    return VocalCorrectionValidateOut(**payload)


__all__ = [
    "CATEGORY_IN_PROGRESS",
    "CATEGORY_MISSING_STEMS",
    "CATEGORY_UNKNOWN_TRACK",
    "VocalCorrectionValidateIn",
    "VocalCorrectionValidateOut",
    "VocalsAnalyzeIn",
    "VocalsAnalyzeOut",
    "VocalsRegionOut",
    "VocalsStatusOut",
    "analyze_vocals",
    "get_vocal_status",
    "router",
    "validate_vocal_correction",
]
