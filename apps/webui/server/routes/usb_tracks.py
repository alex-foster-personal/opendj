"""Play from USB: a stick's rekordbox library and its files (USBPLAY-03/04/06/07).

GET       /api/v1/usb/volumes/{volume_id}/library   - the stick's tracks, playlists, history
GET       /api/v1/usb/tracks/{track_id}              - TrackOut, the library's own shape
GET|HEAD  /api/v1/usb/tracks/{track_id}/audio        - the file, Range/206 like the library
GET       /api/v1/usb/tracks/{track_id}/anlz         - the stick's own ANLZ, library /anlz shape
GET       /api/v1/usb/tracks/{track_id}/hot-cues     - eight slots, library shape, read only
GET       /api/v1/usb/tracks/{track_id}/artwork      - the pdb's jpg (s) or its _m sibling

Every handler resolves through :mod:`apps.sync.usb.stick_library`, which
finds the stick by VolumeUUID in a FRESH discovery scan (never a path the
client sent) and refuses any file that resolves outside the mount. The scan
comes from :mod:`.usb_volumes`, so the ``usb.export`` gate, the sandbox
refusal and the ``usb_volume_discovery_unavailable`` 503 are the SAME ones
the volume list answers. Read only: no write route exists under
``/usb/tracks`` (USBPLAY-08).
"""

from __future__ import annotations

import errno
import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, assert_never, get_args

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from apps.shared import audio_quality, runtime_policy
from apps.shared.bounded_file_open import AUDIO_ACCESS_TIMEOUT_S, probe_readable_byte
from apps.sync.usb.pioneer.anlz_track import (
    StickAnlzError,
    StickAnlzFileMissing,
    StickAnlzPathRefused,
    StickAnlzUnreadable,
    StickTrackAnalysis,
    read_stick_track_analysis,
)
from apps.sync.usb.stick_library import (
    ArtworkSize,
    MountedVolume,
    ResolvedStickTrack,
    StickError,
    StickErrorCode,
    StickLibrary,
    StickTrack,
    VolumeScan,
    is_canonical_volume_uuid,
    open_stick_library,
    resolve_stick_track,
    stick_anlz_file,
    stick_artwork_file,
    stick_audio_file,
    stick_audio_path,
)

from .. import rb_vendor
from ..etag import compute_etag
from ..models import TrackOut
from .rb_assets import _CACHE_ANLZ, _etag_matches
from .rb_assets_audio import _BLOCKED_ACCESS_ERRNOS
from .rb_hot_cues import AnlzCueOut, HotCueSlot, HotCueSlotOut
from .usb_gate import usb_export_gate
from .usb_volumes import (
    UsbCapabilityErrorOut,
    UsbDiscoveryUnavailable,
    UsbVolume,
    _scan_volumes,
    _system_discovery,
)

router = APIRouter(prefix="/usb", tags=["usb"])

_VOLUME_ID_PREFIX = "vol:"
_PATH_VOLUME_ID_PREFIX = "path:"
_CACHE_AUDIO = "no-store"
# A re-export can put different art behind the same pdb artwork path, so
# the browser keeps the bytes but revalidates (FileResponse sends an ETag).
_CACHE_ARTWORK = "private, no-cache"


# ----- wire models --------------------------------------------------------


class UsbStickErrorDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: StickErrorCode | Literal["AUDIO_ACCESS_BLOCKED", "ANALYSIS_NOT_FOUND"]
    message: str
    #: Which stick, for the "Stick removed" toast and for agents.
    volume_uuid: str | None = None
    volume_id: str | None = None
    #: Why a path was refused (USB_PATH_OUTSIDE_VOLUME) or a volume id has
    #: no usable UUID (USB_VOLUME_HAS_NO_UUID).
    reason: str | None = None


class UsbStickErrorOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    detail: UsbStickErrorDetail


class UsbStickTrackOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    pdb_id: int
    title: str
    artist: str | None
    album: str | None
    genre: str | None
    key: str | None
    bpm: float | None
    duration_s: float | None
    rating: int
    #: Stick-relative, exactly as the pdb stores it (never trimmed).
    file_path: str
    #: The pdb names an ANLZ path; not a stat of the stick.
    has_analysis: bool
    has_artwork: bool
    date_added: str | None


class UsbStickPlaylistOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    pdb_id: int
    name: str
    parent_id: str | None
    is_folder: bool
    sort_order: int
    track_ids: list[str]


class UsbStickHistoryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    track_ids: list[str]


class UsbStickCountsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    tracks: int
    playlists: int
    playlist_entries: int
    history_playlists: int


class UsbStickLibraryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    volume_id: str
    volume_uuid: str
    name: str
    #: The real mount path, untrimmed (a volume name can end in a space).
    mount_path: str
    tracks: list[UsbStickTrackOut]
    playlists: list[UsbStickPlaylistOut]
    history: list[UsbStickHistoryOut]
    counts: UsbStickCountsOut
    #: Wall time this request spent resolving the stick and its library.
    read_ms: float
    #: True when the export.pdb parse came from cache (pdb size+mtime unchanged).
    cache_hit: bool


def _error_doc(description: str) -> dict[str, object]:
    return {"model": UsbStickErrorOut, "description": description}


_STICK_RESPONSES: dict[int | str, dict[str, object]] = {
    403: _error_doc("USB_PATH_OUTSIDE_VOLUME: the pdb path resolves outside the stick "
                    "or outside the directory/extension this route may serve"),
    404: _error_doc("USB_STICK_NOT_MOUNTED (carries volume_uuid), USB_TRACK_NOT_FOUND, "
                    "USB_FILE_MISSING or, for anlz and hot-cues, ANALYSIS_NOT_FOUND "
                    "(the track's ANLZ files hold no readable tag)"),
    409: _error_doc("USB_VOLUME_HAS_NO_UUID: the volume id is path-based or its UUID "
                    "is not canonical, so its stick ids would not be stable"),
    422: _error_doc("USB_TRACK_ID_INVALID or USB_VOLUME_ID_INVALID"),
    503: {
        "model": UsbCapabilityErrorOut | UsbStickErrorOut,
        "description": (
            "usb_volume_discovery_unavailable (the volume list's own refusal), "
            "USB_STICK_ACCESS_BLOCKED (reading export.pdb was refused or did not "
            "return within AUDIO_ACCESS_TIMEOUT_S) or, for audio, AUDIO_ACCESS_BLOCKED"
        ),
    },
}


# ----- resolution ---------------------------------------------------------


def _http_status(code: StickErrorCode) -> int:
    match code:
        case "USB_TRACK_ID_INVALID" | "USB_VOLUME_ID_INVALID":
            return 422
        case "USB_VOLUME_HAS_NO_UUID":
            return 409
        case "USB_STICK_NOT_MOUNTED" | "USB_TRACK_NOT_FOUND" | "USB_FILE_MISSING":
            return 404
        case "USB_PATH_OUTSIDE_VOLUME":
            return 403
        case "USB_STICK_ACCESS_BLOCKED":
            return 503
        case _:
            assert_never(code)


@contextmanager
def _stick_errors() -> Iterator[None]:
    """Typed refusals -> HTTP. Discovery refusals keep the volume list's
    exact ``{"detail": {code, reason, ui_title?}}`` body."""
    try:
        yield
    except StickError as exc:
        raise HTTPException(status_code=_http_status(exc.code), detail=exc.to_detail()) from exc
    except UsbDiscoveryUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.to_detail()) from exc


def _stick_scan(request: Request) -> VolumeScan:
    """The gate and discovery are checked here, on every request, cache hit
    or not; the scan itself runs only when the resolver needs to (re)bind."""
    discovery = _system_discovery(usb_export_gate=usb_export_gate(request))

    def scan() -> list[MountedVolume]:
        # FORCED: the listing's throttled cache can be seconds stale, and a
        # binding must reflect what is mounted now. Real rows only: simulated
        # volumes are never merged here (and their ids are "sim:", never "vol:").
        real = _scan_volumes(discovery=discovery, force=True, include_simulated=False)
        return [_mounted_volume(volume) for volume in real]

    return scan


def _mounted_volume(volume: UsbVolume) -> MountedVolume:
    if volume.mount_path is None:
        raise RuntimeError(f"discovery listed {volume.id} with no mount path")
    return MountedVolume(
        volume_id=volume.id,
        volume_uuid=_canonical_uuid_of(volume.id),
        name=volume.name,
        mount_path=Path(volume.mount_path),
    )


def _canonical_uuid_of(volume_id: str) -> str | None:
    if not volume_id.startswith(_VOLUME_ID_PREFIX):
        return None
    candidate = volume_id.removeprefix(_VOLUME_ID_PREFIX)
    return candidate if is_canonical_volume_uuid(candidate) else None


def _volume_uuid_from_id(volume_id: str) -> str:
    if volume_id.startswith(_PATH_VOLUME_ID_PREFIX):
        raise StickError(
            "USB_VOLUME_HAS_NO_UUID",
            "this volume reported no VolumeUUID, so its tracks cannot have stable ids",
            volume_id=volume_id,
            reason="no_volume_uuid",
        )
    if not volume_id.startswith(_VOLUME_ID_PREFIX):
        raise StickError(
            "USB_VOLUME_ID_INVALID",
            f"{volume_id!r} is not a volume id from GET /api/v1/usb/volumes",
            volume_id=volume_id,
        )
    volume_uuid = _canonical_uuid_of(volume_id)
    if volume_uuid is None:
        raise StickError(
            "USB_VOLUME_HAS_NO_UUID",
            "the volume's UUID is not uppercase-hex 8-4-4-4-12",
            volume_id=volume_id,
            reason="volume_uuid_not_canonical",
        )
    return volume_uuid


def _resolve_track(request: Request, track_id: str) -> ResolvedStickTrack:
    return resolve_stick_track(track_id, _stick_scan(request))


# ----- library ------------------------------------------------------------


@router.get(
    "/volumes/{volume_id}/library",
    response_model=UsbStickLibraryOut,
    responses=_STICK_RESPONSES,
)
def get_usb_stick_library(volume_id: str, request: Request) -> UsbStickLibraryOut:
    """The stick's rekordbox library from ``export.pdb`` alone (no ANLZ read).

    Cached per VolumeUUID while ``export.pdb``'s size and mtime are
    unchanged; ``cache_hit`` and ``read_ms`` say which path this was.
    """
    started = time.perf_counter()
    with _stick_errors():
        # The gate first, as on every track route: a gated-off build answers
        # the volume list's 503 whatever the volume id looks like.
        scan = _stick_scan(request)
        opened = open_stick_library(_volume_uuid_from_id(volume_id), scan)
    library = opened.library
    return UsbStickLibraryOut(
        volume_id=opened.stick.volume_id,
        volume_uuid=opened.stick.volume_uuid,
        name=opened.stick.name.strip(),
        mount_path=str(opened.stick.mount),
        tracks=[_track_out(track) for track in library.tracks],
        playlists=[
            UsbStickPlaylistOut(
                id=p.id,
                pdb_id=p.pdb_id,
                name=p.name,
                parent_id=p.parent_id,
                is_folder=p.is_folder,
                sort_order=p.sort_order,
                track_ids=list(p.track_ids),
            )
            for p in library.playlists
        ],
        history=[
            UsbStickHistoryOut(id=h.id, name=h.name, track_ids=list(h.track_ids))
            for h in library.history
        ],
        counts=UsbStickCountsOut(
            tracks=len(library.tracks),
            playlists=len(library.playlists),
            playlist_entries=library.playlist_entry_count,
            history_playlists=len(library.history),
        ),
        read_ms=(time.perf_counter() - started) * 1000.0,
        cache_hit=opened.cache_hit,
    )


def _track_out(track: StickTrack) -> UsbStickTrackOut:
    return UsbStickTrackOut(
        id=track.id,
        pdb_id=track.pdb_id,
        title=track.title,
        artist=track.artist,
        album=track.album,
        genre=track.genre,
        key=track.key,
        bpm=track.bpm,
        duration_s=track.duration_s,
        rating=track.rating,
        file_path=track.file_path,
        has_analysis=track.has_analysis,
        has_artwork=track.has_artwork,
        date_added=track.date_added,
    )


# ----- one track ----------------------------------------------------------


@router.get("/tracks/{track_id}", response_model=TrackOut, responses=_STICK_RESPONSES)
def get_usb_track(track_id: str, request: Request, response: Response) -> TrackOut:
    """The library's TrackOut for a stick track, so the deck loads it unchanged.

    The flags predict the stick routes: no lyrics, auto-cues or stems route
    exists under ``/usb/tracks``, so those are False; ``has_rb_mapping`` is
    False because ``/hot-cues`` is read only and ``rb-meta`` cannot resolve a
    stick id; ``artwork_available`` is True only when both served sizes
    exist. ``/anlz`` and ``/hot-cues`` answer for every track whose export
    row names no analysis (empty) or a readable one (the stick's own).
    """
    with _stick_errors():
        resolved = _resolve_track(request, track_id)
        audio_path = stick_audio_path(resolved)
        artwork_available = _artwork_available(resolved)
    track = resolved.track
    library = resolved.library
    exported_at = _pdb_modified_iso(library)
    response.headers["ETag"] = compute_etag(
        track.id, exported_at, f"{library.pdb_size}:{library.pdb_mtime_ns}:{artwork_available}"
    )
    return TrackOut(
        stable_id=track.id,
        title=track.title or None,
        artist=track.artist,
        album=track.album,
        duration_ms=_duration_ms(track.duration_s),
        bpm=track.bpm,
        key=track.key,
        rating=track.rating,
        file_path=str(audio_path),
        play_count=track.play_count,
        created_at=track.date_added or exported_at,
        updated_at=exported_at,
        has_rb_mapping=False,
        lyrics_available=False,
        auto_cues_available=False,
        stems_available=False,
        artwork_available=artwork_available,
    )


def _artwork_available(resolved: ResolvedStickTrack) -> bool:
    """Predicts GET /artwork for the sizes the UI asks for (s and m)."""
    if not resolved.track.has_artwork:
        return False
    try:
        for size in ("s", "m"):
            stick_artwork_file(resolved, size)
    except StickError as exc:
        if exc.code in ("USB_FILE_MISSING", "USB_PATH_OUTSIDE_VOLUME"):
            return False
        raise
    return True


def _pdb_modified_iso(library: StickLibrary) -> str:
    return datetime.fromtimestamp(library.pdb_mtime_ns / 1e9, tz=UTC).isoformat()


def _duration_ms(duration_s: float | None) -> int | None:
    return None if duration_s is None else round(duration_s * 1000)


# ----- analysis -----------------------------------------------------------


def _stick_analysis(resolved: ResolvedStickTrack, points: int) -> StickTrackAnalysis | None:
    """The stick's own ANLZ for this track; None when its export row names no
    analysis (rekordbox never analyzed it, a fact about the track).

    The .DAT is gated through the resolver first, so a refusal carries the
    same typed ``reason`` as the audio and artwork routes; the decoder then
    re-proves containment itself for its .EXT/.2EX siblings.
    """
    analyze_path = resolved.track.analyze_path
    if analyze_path is None:
        return None
    stick_anlz_file(resolved, ".DAT")
    try:
        return read_stick_track_analysis(
            volume_root=resolved.stick.mount, analyze_path=analyze_path, points=points
        )
    except StickAnlzError as exc:
        raise _anlz_refusal(resolved, exc) from exc


def _anlz_refusal(resolved: ResolvedStickTrack, exc: StickAnlzError) -> HTTPException:
    if isinstance(exc, StickAnlzPathRefused):
        status = 403
    elif isinstance(exc, StickAnlzFileMissing | StickAnlzUnreadable):
        status = 404
    else:
        raise TypeError(f"no HTTP status for stick ANLZ refusal {type(exc).__name__}") from exc
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": str(exc), "volume_uuid": resolved.stick.volume_uuid},
    )


def _anlz_payload(
    track_id: str, analysis: StickTrackAnalysis | None, points: int
) -> dict[str, Any]:
    if analysis is None:
        payload = rb_vendor.empty_anlz_payload(track_id, points)
    else:
        payload = {"stable_id": track_id, "points": points, **analysis.payload}
    # The stick's own grid, always (spec decision 5). The PARITY-02 toggle
    # chooses between the LIBRARY's two lanes; a stick has no own lane, and
    # the deck's source-confirmation loop exempts stick ids.
    payload["beatgrid_source"] = "rekordbox"
    payload["beatgrid_own_unavailable_reason"] = None
    return payload


@router.get("/tracks/{track_id}/anlz", responses=_STICK_RESPONSES)
def get_usb_track_anlz(
    track_id: str,
    request: Request,
    points: int = Query(
        runtime_policy.ANLZ_POINTS_DEFAULT,
        ge=runtime_policy.ANLZ_POINTS_MIN,
        le=runtime_policy.ANLZ_POINTS_MAX,
        description="Max length of each waveform band array after downsampling",
    ),
) -> Response:
    """The stick's own waveform / beatgrid / cues / phrases, in the library
    ``/anlz`` shape, decoded from this track's exact ANLZ files.

    ``vocals`` is always ``not_analyzed`` (a PVDI tag is listed in
    ``unreadable_anlz``); ``beatgrid.source`` and ``beatgrid_source`` are
    always ``rekordbox``. Same ``points`` bounds, ETag and
    ``private, no-cache`` revalidation as the library route; the client's
    ``gen`` cache-buster is ignored here as it is there.
    """
    with _stick_errors():
        resolved = _resolve_track(request, track_id)
        analysis = _stick_analysis(resolved, points)
    payload = _anlz_payload(resolved.track.id, analysis, points)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    etag = f'"{hashlib.sha256(body.encode("utf-8")).hexdigest()}"'
    headers = {"Cache-Control": _CACHE_ANLZ, "ETag": etag}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get(
    "/tracks/{track_id}/hot-cues",
    response_model=list[HotCueSlotOut],
    responses=_STICK_RESPONSES,
)
def list_usb_track_hot_cue_slots(track_id: str, request: Request) -> list[HotCueSlotOut]:
    """Eight slots from the stick's own cues, the library's shape. Read only:
    no PUT/DELETE/restore exists here, so a revision is only an identity for
    the slot's current state and edits stay in the deck's session."""
    with _stick_errors():
        resolved = _resolve_track(request, track_id)
        # Smallest waveform: only the cues are used, and they do not depend on it.
        analysis = _stick_analysis(resolved, runtime_policy.ANLZ_POINTS_MIN)
    cues: list[dict[str, Any]] = [] if analysis is None else analysis.payload["cues"]
    return _hot_cue_slots(resolved.track.id, cues)


def _hot_cue_slots(track_id: str, cues: list[dict[str, Any]]) -> list[HotCueSlotOut]:
    hot = [cue for cue in cues if cue["kind"] == "hot_cue"]
    by_slot = {cue["slot"]: cue for cue in hot}
    if len(by_slot) != len(hot):
        raise RuntimeError(f"{track_id} has two hot cues in one slot: {hot}")
    slots: list[HotCueSlotOut] = []
    for slot in get_args(HotCueSlot):
        cue = by_slot.get(slot)
        revision = _hot_cue_revision(track_id, slot, cue)
        slots.append(
            HotCueSlotOut(
                slot=slot,
                cue=None if cue is None else AnlzCueOut(**cue, revision=revision),
                revision=revision,
            )
        )
    return slots


def _hot_cue_revision(track_id: str, slot: str, cue: dict[str, Any] | None) -> str:
    encoded = json.dumps(
        {"stick_track": track_id, "slot": slot, "cue": cue},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ----- files --------------------------------------------------------------


@router.get(
    "/tracks/{track_id}/audio",
    response_class=FileResponse,
    responses=_STICK_RESPONSES,
    operation_id="get_usb_track_audio_api_v1_usb_tracks__track_id__audio_get",
)
@router.head(
    "/tracks/{track_id}/audio",
    response_class=FileResponse,
    responses=_STICK_RESPONSES,
    operation_id="head_usb_track_audio_api_v1_usb_tracks__track_id__audio_head",
)
def get_usb_track_audio(track_id: str, request: Request) -> FileResponse:
    """Stream the stick's file. FileResponse handles Range/206 and HEAD, as
    the library's ``/tracks/{id}/audio`` does.

    A subprocess probe opens the file under ``AUDIO_ACCESS_TIMEOUT_S`` first,
    so a kernel-blocked ``open()`` (a pending macOS Removable Volumes prompt)
    answers 503 ``AUDIO_ACCESS_BLOCKED`` instead of hanging the worker.
    """
    with _stick_errors():
        resolved = _resolve_track(request, track_id)
        audio = stick_audio_file(resolved)
    probe = probe_readable_byte(audio.path, timeout_s=AUDIO_ACCESS_TIMEOUT_S)
    if probe.outcome == "timeout" or (
        probe.outcome == "error" and probe.errno in _BLOCKED_ACCESS_ERRNOS
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AUDIO_ACCESS_BLOCKED",
                "message": (
                    f"opening {audio.path} was refused or did not return within "
                    f"{AUDIO_ACCESS_TIMEOUT_S:.0f}s ({probe.outcome})"
                ),
                "volume_uuid": resolved.stick.volume_uuid,
            },
        )
    if probe.outcome == "error" and probe.errno in (errno.ENOENT, errno.ENOTDIR):
        raise HTTPException(
            status_code=404,
            detail=StickError(
                "USB_FILE_MISSING",
                f"audio file {resolved.track.file_path} went away before it could be opened",
                volume_uuid=resolved.stick.volume_uuid,
            ).to_detail(),
        )
    if probe.outcome == "error":
        raise OSError(probe.errno or 0, f"probing {audio.path} failed: {probe.message}")
    quality = audio_quality.classify(str(audio.path), _duration_ms(resolved.track.duration_s))
    return FileResponse(
        audio.path,
        media_type=audio.media_type,
        headers={
            "Cache-Control": _CACHE_AUDIO,
            "X-Audio-Kind": "local",
            "X-Audio-Venue": quality.venue.key if quality.venue else "",
            "X-Audio-Source": "usb-stick",
        },
    )


@router.get(
    "/tracks/{track_id}/artwork",
    response_class=FileResponse,
    response_model=None,
    responses=_STICK_RESPONSES,
)
def get_usb_track_artwork(
    track_id: str,
    request: Request,
    size: ArtworkSize = Query(  # noqa: B008  # FastAPI DI
        "s", description="s=80x80 browser rows; m and orig = the 240x240 _m jpg"
    ),
) -> FileResponse:
    """The pdb's pre-rendered jpg: ``s`` as named, ``m``/``orig`` its ``_m``
    sibling (the largest rendering rekordbox writes to a stick)."""
    with _stick_errors():
        path = stick_artwork_file(_resolve_track(request, track_id), size)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": _CACHE_ARTWORK})


__all__ = [
    "UsbStickErrorOut",
    "UsbStickLibraryOut",
    "get_usb_stick_library",
    "get_usb_track",
    "get_usb_track_anlz",
    "get_usb_track_artwork",
    "get_usb_track_audio",
    "list_usb_track_hot_cue_slots",
    "router",
]
