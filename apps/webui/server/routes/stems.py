"""Read-only endpoints for validated precomputed Demucs stem bundles.

The application integrator mounts :data:`router` at ``/api/v1``.  The router
does not run Demucs or mutate files: every request reloads and validates the
stored artifact before it exposes either the manifest or a container-typed audio response.
The manifest GET answers HTTP 200 unavailable when no bundle exists; the
part GET still 404s when there is no file to stream.

Stem A/B/C labelling tasks (issue #11193) expose clip URLs under this router's
``GET .../stems/{part}`` path; see ``apps.webui.server.routes.label_tasks``.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from apps.cloud import stem_hydration, stem_index
from apps.cloud.eviction import HydrationError
from apps.cloud.stem_source import STEM_BUNDLE_NOT_INDEXED
from apps.stems.artifacts import StemArtifactError, StemBundleNotFoundError, load_stem_bundle

from .stems_bundle import _hold_for_deck, _stem_roots, _stems_dir
from .stems_files import _stem_file_response, _stem_manifest_response

# Re-exported: tests, stems_assets.py, and sibling modules reach these through stems.
from .stems_hydration import (  # noqa: F401
    _INFLIGHT,
    _INFLIGHT_FILE_TOTALS,
    _INFLIGHT_LOCK,
    _LAST_HYDRATE_ERROR,
    CAP_UNNAMED_RECHECK_S,
    STEM_PART_HYDRATE_WAIT_S,
    _cap_refusal_expiry,
    _enqueue_hydration,
    _hub_endpoint_from_message,
    _HydrateError,
    _log_hydration_failure,
    _raise_monthly_cap_refusal,
    _raise_stem_index_corrupt,
    _recorded_hydration_error,
    _remember_hydrate_error,
)
from .stems_models import (
    STEM_HYDRATION_ERROR_RESPONSES,
    STEM_PART_RESPONSES,
    StemHydrationProgressOut,
    StemManifestOut,
    StemPartOut,
    StemStateOut,
    StemTrackState,
    StemUnavailableOut,
)
from .stems_state import (  # noqa: F401
    _armed_index_state,
    _bundle_landed,
    _hydration_arming,
    _hydration_in_flight,
    _hydration_progress,
    _index_proves_absent,
    _shared_hydration_dir,
    _state_progress,
    _this_track_fetching,
    _written_so_far,
)
from .stems_waveform import (  # noqa: F401
    StemWaveformOut,
    _build_resolved_waveform,
    _resolve_waveform_inputs,
    _retry_swaps,
    _stem_waveform_response,
)

router = APIRouter(prefix="/tracks", tags=["stems"])
MOUNT_UNDER_API_PREFIX = True

log = logging.getLogger(__name__)

#: Longest a state read retries while hydrates on its lock stripe keep
#: replacing bundles (each replacement is a rename plus a verify, not a
#: download). Reached only if the stripe never settles; then, with no fetch of
#: this track running, the first read stands.
STEM_STATE_SETTLE_S: float = 2.0
#: Pause between those retries.
STEM_STATE_SETTLE_RETRY_S: float = 0.005
def _read_bundle(stable_id: str, stems_dir: Path, request: Request) -> str | None:
    """``"local"`` when the bundle loads, the reason when it is invalid, else ``None``."""
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir, roots=_stem_roots(request))
    except StemBundleNotFoundError:
        return None
    except StemArtifactError as exc:
        return str(exc)
    return "local"


def _local_bundle_read(stable_id: str, stems_dir: Path, request: Request) -> str | None:
    """Read the bundle on this machine: ``"local"``, ``"fetching"``, the
    reason it is invalid, or ``None`` when there is none.

    After a read that loads, the first question for every other outcome is
    whether THIS track has a fetch running or queued: this route's, or any
    live process's owner marker, which a hydrate publishes before it waits for
    any lock. If so the read is ``"fetching"``, whatever the bundle looked
    like. Otherwise a failed read that could be stale, an invalid bundle or a
    missing one with a fetch error recorded, is decided again:

    * The bundle is read again and the answer is kept only if no
      hydrate on its lock stripe replaced a bundle meanwhile: the stripe's
      publish generation (a seqlock, see
      :func:`stem_hydration.hydrate_generation_path`) was even and unchanged
      across the read. An odd generation with the stripe's locks free means a
      publisher died mid-publish, so the read is made under the locks.

    A clean miss is decided the same way: a hydrate can start, land the bundle
    and finish inside the first read, leaving the owner check with nothing to
    see, so the miss is only trusted once the generation has held still.
    """
    first = _read_bundle(stable_id, stems_dir, request)
    if first == "local":
        # A hydrate never replaces a bundle that loads (it answers
        # already_local, or keeps the winner), so this cannot be stale.
        return first
    shared_dir = _shared_hydration_dir(stable_id, request)
    if _this_track_fetching(stable_id, shared_dir):
        return "fetching"
    if shared_dir is None:
        return first
    return _settled_failed_read(stable_id, stems_dir, request, shared_dir, first)


def _settled_failed_read(
    stable_id: str, stems_dir: Path, request: Request, shared_dir: Path, first: str | None
) -> str | None:
    """Re-read until no publish on the stripe overlaps the read (the answer),
    or this track starts fetching (``"fetching"``).

    If the stripe never settles within :data:`STEM_STATE_SETTLE_S` and no
    fetch of this track is running, the publishes are another track's, so
    ``first`` stands (``None`` lets a recorded fetch error answer).
    """
    deadline = time.monotonic() + STEM_STATE_SETTLE_S
    while not _this_track_fetching(stable_id, shared_dir):
        settled = _read_settled_bundle(stable_id, stems_dir, request, shared_dir)
        if not isinstance(settled, _Unsettled):
            # A hydrate of this track may have started during that read: its
            # marker comes first, the generation moves only when it publishes.
            if settled != "local" and _this_track_fetching(stable_id, shared_dir):
                return "fetching"
            return settled
        if time.monotonic() >= deadline:
            # A hydrate of this track may have started during that last read.
            return "fetching" if _this_track_fetching(stable_id, shared_dir) else first
        time.sleep(STEM_STATE_SETTLE_RETRY_S)
    return "fetching"


class _Unsettled:
    """A read that a publish on the stripe overlapped: not an answer."""


_UNSETTLED: Final = _Unsettled()


def _read_settled_bundle(
    stable_id: str, stems_dir: Path, request: Request, data_dir: Path
) -> str | _Unsettled | None:
    """One seqlock-checked read, or :data:`_UNSETTLED` (also when the
    generation cannot be read, so the caller retries until its deadline)."""
    try:
        return _read_against_generation(stable_id, stems_dir, request, data_dir)
    except HydrationError:
        return _UNSETTLED


def _read_against_generation(
    stable_id: str, stems_dir: Path, request: Request, data_dir: Path
) -> str | _Unsettled | None:
    before = stem_hydration.read_hydrate_generation(data_dir, stable_id)
    if before % 2 == 0:
        result = _read_bundle(stable_id, stems_dir, request)
        if stem_hydration.read_hydrate_generation(data_dir, stable_id) == before:
            return result
        return _UNSETTLED
    with stem_hydration.hydrate_locks_within(data_dir, stable_id, 0) as held:
        if held:
            return _read_bundle(stable_id, stems_dir, request)
    return _UNSETTLED
def _checked_after_local_read(
    stable_id: str, stems_dir: Path, request: Request, local: str | None
) -> tuple[str | None, bool, frozenset[str] | None]:
    """Returns the local read, whether this track is fetching, and progress suffixes.

    The owner is checked again after the local read: a bulk or runner hydrate
    can publish its owner marker after that read's own check, and a clean miss
    must not be reported as cloud while that fetch is already running. A read
    that saw a fetch is read again, since that fetch may have finished. Any
    other answer was already settled by :func:`_local_bundle_read`, so it is
    not read again: a second full read would repeat its validations on every
    poll.

    When the outcome is ``fetching``, the third element lists owner-marker
    suffixes for progress temp-dir matching (one owners-dir scan per read,
    shared with :func:`_stem_state`). :func:`_this_track_fetching` stays the
    bool helper for settle loops; this path must not call it and then scan
    owners again for the same answer.
    """
    if local == "local":
        return local, False, None
    shared_dir = _shared_hydration_dir(stable_id, request)

    def _snapshot_suffixes() -> frozenset[str]:
        if shared_dir is None:
            return frozenset()
        return stem_hydration.hydrate_owner_snapshot(shared_dir, stable_id)[1]

    if _hydration_in_flight(stable_id):
        return "fetching", True, _snapshot_suffixes()
    if shared_dir is not None:
        owner_alive, sfx = stem_hydration.hydrate_owner_snapshot(shared_dir, stable_id)
        if owner_alive:
            return "fetching", True, sfx
    if local != "fetching":
        return local, False, None
    local = _local_bundle_read(stable_id, stems_dir, request)
    if local != "fetching":
        return local, False, None
    return "fetching", True, _snapshot_suffixes()


def _stem_state(stable_id: str, request: Request) -> StemStateOut:
    """Name the track's stem state WITHOUT starting or re-arming anything.

    Deliberately reads ``app.state`` directly instead of calling
    :func:`_hydration_deps`, which raises and may re-arm hydration: a state
    read that changes the state it reports is not a read.
    """
    stems_dir = _stems_dir(request)
    deck_open = stem_hydration.OPEN_DECKS.is_open(stable_id)
    unarmed_reason, data_dir, armed = _hydration_arming(request.app.state)

    def _out(
        name: StemTrackState,
        message: str,
        *,
        progress: StemHydrationProgressOut | None = None,
        error_code: str | None = None,
    ) -> StemStateOut:
        return StemStateOut(
            stable_id=stable_id,
            state=name,
            message=message,
            hydration_armed=armed,
            progress=progress,
            error_code=error_code,
            deck_open=deck_open,
        )

    local, owner_fetching, progress_suffixes = _checked_after_local_read(
        stable_id, stems_dir, request, _local_bundle_read(stable_id, stems_dir, request)
    )
    if local == "local":
        return _out("local", "stem bundle is on this machine")
    if local is not None and local != "fetching":
        return _out("error", local, error_code="STEM_ARTIFACT_INVALID")

    if local == "fetching" or owner_fetching:
        try:
            progress = _state_progress(
                stable_id,
                stems_dir,
                data_dir,
                attempt_suffixes=progress_suffixes,
            )
        except stem_index.StemIndexError as exc:
            # A corrupt index is named, never read as a fetch with no progress.
            _raise_stem_index_corrupt(exc, Path(data_dir))
        return _out(
            "fetching",
            "fetching the stem bundle from the cloud",
            progress=progress,
        )
    recorded = _recorded_hydration_error(stable_id)
    if recorded is not None:
        return _out("error", recorded.message, error_code=recorded.code)
    if unarmed_reason is not None:
        if _index_proves_absent(stable_id, data_dir):
            return _out("none", "no stem bundle on this machine or in the cloud")
        return _out("error", unarmed_reason, error_code="STEM_HYDRATION_NOT_ARMED")
    if not armed:
        return _out(
            "none",
            "no stem bundle on this machine, and cloud stems are not configured here",
        )
    name, message, code = _armed_index_state(stable_id, Path(data_dir), stems_dir, request)
    return _out(name, message, error_code=code)
@router.get(
    "/{stable_id}/stems",
    response_model=StemManifestOut | StemUnavailableOut,
    responses=STEM_HYDRATION_ERROR_RESPONSES,
)
def get_stem_manifest(
    stable_id: str, request: Request
) -> StemManifestOut | StemUnavailableOut:
    """Return a stored v1 manifest after alignment, or HTTP 200 unavailable when none exists.

    Never blocks on R2: if the bundle is missing locally but the R2 index
    has it, this ENQUEUES a background hydration and returns immediately
    with ``hydrating=True`` (D2). A prior hydration attempt that already
    failed is reported LOUD as HTTP 502, never silently folded into the
    ordinary "no bundle" empty state.
    """
    withdraw = _hold_for_deck(stable_id, request)
    try:
        out = _stem_manifest_response(stable_id, request)
    except BaseException:
        withdraw()
        raise
    if isinstance(out, StemUnavailableOut):
        # No bundle was served, so this read holds none. A hydration it
        # enqueued hands its own served lease to the deck that asked once the
        # bundle lands (``hand_off_to_deck``); that one is the deck load's
        # (Codex on #6820).
        withdraw()
    return out


@router.get("/{stable_id}/stems/state", response_model=StemStateOut)
def get_stem_state(stable_id: str, request: Request) -> StemStateOut:
    """Name this track's stem state. Read-only: never starts a download.

    Agent-native parity for what the deck's stem row shows. Registered BEFORE
    the ``{part}`` route on purpose, which would otherwise read ``state`` as a
    stem part name.
    """
    return _stem_state(stable_id, request)


@router.post(
    "/{stable_id}/stems/hydrate",
    response_model=StemStateOut,
    responses=STEM_HYDRATION_ERROR_RESPONSES,
)
def hydrate_stem_bundle(stable_id: str, request: Request) -> StemStateOut:
    """Fetch this track's stem bundle from the cloud now, and retry a failure.

    The manifest route records a failed fetch and answers 502 from then on so
    a hot loop cannot hammer R2 with the same doomed request. This is the
    explicit way back: it drops that record and starts a fresh fetch through
    the same single-flight path, so the cache floor and the evictor apply
    exactly as they do for a deck load. A bundle already on disk is a no-op.
    Returns at once with the resulting state; poll ``.../stems/state``.
    """
    stems_dir = _stems_dir(request)
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir, roots=_stem_roots(request))
    except StemBundleNotFoundError:
        with _INFLIGHT_LOCK:
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
        _enqueue_hydration(stable_id, request)
    except StemArtifactError as exc:
        # An invalid bundle on disk is repaired by fetching the cloud copy,
        # which replaces it; with no cloud copy there is nothing to retry.
        invalid = HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        )
        with _INFLIGHT_LOCK:
            _LAST_HYDRATE_ERROR.pop(stable_id, None)
        try:
            enqueued = _enqueue_hydration(stable_id, request)
        except HTTPException as not_indexed:
            detail = not_indexed.detail
            if not isinstance(detail, dict) or detail.get("code") != STEM_BUNDLE_NOT_INDEXED:
                raise
            raise invalid from exc
        if enqueued is None:
            raise invalid from exc
    return _stem_state(stable_id, request)
@router.get(
    "/{stable_id}/stems/{part}",
    response_class=StreamingResponse,
    responses=STEM_PART_RESPONSES,
)
def get_stem_file(stable_id: str, part: str, request: Request) -> StreamingResponse:
    """Stream one validated stem part using the bundle's container media type.

    Unlike the manifest route, this one DOES wait (bounded) for an in-flight
    hydration: a part request means the caller already decided it wants
    these bytes. A prior failure is surfaced immediately as HTTP 502; a
    fresh attempt that does not finish within
    :data:`STEM_PART_HYDRATE_WAIT_S` answers HTTP 503 so the caller can
    retry rather than hang forever on this one request.
    """
    withdraw = _hold_for_deck(stable_id, request)
    try:
        return _stem_file_response(stable_id, part, request)
    except BaseException:
        withdraw()
        raise
@router.get(
    "/{stable_id}/stems/{part}/waveform",
    response_model=StemWaveformOut,
    responses=STEM_PART_RESPONSES,
)
def get_stem_waveform(stable_id: str, part: str, request: Request) -> StemWaveformOut:
    """Return a downsampled mono peak envelope for one validated stem part."""
    withdraw = _hold_for_deck(stable_id, request)
    try:
        return _stem_waveform_response(stable_id, part, request)
    except BaseException:
        withdraw()
        raise


@router.post("/{stable_id}/stems/deck-open")
def mark_stem_deck_open(stable_id: str, request: Request) -> dict[str, str]:
    """A deck has this bundle open. Protects it from eviction until closed.

    Agent-native parity for the deck-load lifecycle: there is no other
    request boundary that tells the eviction path "a deck is playing this
    right now, do not free its bundle regardless of recency" (D3). Refcounted
    (:class:`apps.cloud.stem_hydration.OpenDeckRegistry`), so two open
    references (e.g. two decks on the same track) both need closing.
    """
    _hold_for_deck(stable_id, request, deck_open=True)
    return {"status": "ok", "stable_id": stable_id, "open": "true"}


@router.post("/{stable_id}/stems/deck-close")
def mark_stem_deck_closed(stable_id: str) -> dict[str, str]:
    """Release one deck-open reference. See :func:`mark_stem_deck_open`."""
    stem_hydration.OPEN_DECKS.mark_closed(stable_id)
    return {"status": "ok", "stable_id": stable_id, "open": "false"}
__all__ = [
    "STEM_HYDRATION_ERROR_RESPONSES",
    "STEM_PART_HYDRATE_WAIT_S",
    "StemHydrationProgressOut",
    "StemManifestOut",
    "StemPartOut",
    "StemStateOut",
    "StemUnavailableOut",
    "StemWaveformOut",
    "router",
]
