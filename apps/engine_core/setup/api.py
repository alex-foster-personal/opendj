"""First-run setup HTTP surface -- one endpoint per wizard step.

AGENT-NATIVE PARITY is the hard constraint here: every step the wizard walks
a human through is a request an agent can make on its own, in the same
order, with the same refusals. There is no browser-only state.

    GET  /api/v1/setup/status            is the library empty, what is here
    GET  /api/v1/setup/detect/rekordbox  what is installed, real paths
    POST /api/v1/setup/import            run the import AS A JOB
    POST /api/v1/setup/dismiss           skip (or un-skip) the wizard
    GET  /api/v1/setup/stems             can stems analysis run at all

Refusals carry ``{"code", "message"}`` so a caller can branch on the code
instead of pattern-matching prose. The codes are the ones in
:mod:`apps.engine_core.setup.detect`, and they stay distinct: a missing
rekordbox, an unreachable key and a failed decrypt are three problems.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from apps.engine_core.jobs.api import JobOut
from apps.engine_core.jobs.store import JobStore
from apps.engine_core.setup import detect, record
from apps.engine_core.setup.importer import STAGES
from apps.engine_core.setup.jobs import (
    SETUP_IMPORT_KIND,
    SetupPayloadError,
    build_argv,
)

router = APIRouter(prefix="/setup", tags=["setup"])

#: Statuses a setup import can still be in. A second import while one of
#: these is live would race the first one's writes into state.db.
_LIVE_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "cancelling"}
)

#: What the stems step says when the infrastructure is not there. Spelled
#: once, asserted by the tests, and rendered verbatim by the wizard -- the
#: house rule is that an unavailable feature says so rather than pretending.
STEMS_UNAVAILABLE_MESSAGE: str = "stems analysis not yet available"


# ----- wire models --------------------------------------------------------
class FileProbeOut(BaseModel):
    """One real path and whether it is actually there."""

    path: str
    exists: bool
    size_bytes: int | None = None
    modified_at: str | None = None


class RekordboxDetectionOut(BaseModel):
    """The 'detect rekordbox' step, reported without touching the install."""

    installed: bool
    live_db: FileProbeOut
    share_dir: FileProbeOut
    working_copy: FileProbeOut
    plain_copy: FileProbeOut
    key_available: bool
    key_detail: str
    import_source: str | None = None
    import_source_encrypted: bool | None = None
    blockers: list[str] = Field(default_factory=list)
    rekordbox_running: bool


class SetupStatusOut(BaseModel):
    """Everything the wizard needs to decide whether to show itself."""

    library_empty: bool
    tracks: int
    playlists: int
    state_db: FileProbeOut
    data_dir: str
    dismissed: bool
    should_show_wizard: bool
    stages: list[str]
    last_import: dict[str, Any] | None = None
    rekordbox: RekordboxDetectionOut


class SetupImportIn(BaseModel):
    """Import options. Both are the same knobs the CLI exposes."""

    source: str | None = Field(
        default=None,
        description=(
            "explicit rekordbox database to read; omit to auto-detect"
        ),
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="ingest at most N tracks (smoke-test aid)",
    )


class SetupDismissIn(BaseModel):
    dismissed: bool = True


class StemTierOut(BaseModel):
    """One real separation rung, straight out of apps.stems.tiers."""

    key: str
    name: str
    where: str
    availability: str
    unavailable_because: str


class StemsSetupOut(BaseModel):
    """Whether the wizard's stems step can actually start anything.

    ``available`` false is the honest answer while nothing can run a
    library-wide separation pass; ``reason`` is what the UI shows. The
    ``tiers`` list is real data either way, never a placeholder.
    """

    available: bool
    reason: str
    per_track_endpoint: str
    library_scan_endpoint: str | None = None
    tiers: list[StemTierOut]


# ----- helpers ------------------------------------------------------------
def _data_dir(request: Request) -> Path:
    cfg = getattr(request.app.state, "engine_cfg", None)
    if cfg is None:
        raise RuntimeError(
            "engine config is not mounted on app.state.engine_cfg; setup "
            "cannot guess which data dir it is being asked about"
        )
    return Path(cfg.data_dir)


def _store(request: Request) -> JobStore:
    store = getattr(request.app.state, "jobs_store", None)
    if store is None:
        raise RuntimeError(
            "engine jobs store is not mounted on app.state.jobs_store"
        )
    return store


def _refuse(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(
        status_code=http_status, detail={"code": code, "message": message}
    )


def _detection_out(data_dir: Path) -> RekordboxDetectionOut:
    found = detect.detect_rekordbox(data_dir)
    return RekordboxDetectionOut(
        **found.to_dict(), rekordbox_running=detect.rekordbox_is_running()
    )


def _status_out(data_dir: Path) -> SetupStatusOut:
    counts = detect.library_counts(data_dir)
    saved = record.read(data_dir)
    return SetupStatusOut(
        library_empty=counts.empty,
        tracks=counts.tracks,
        playlists=counts.playlists,
        state_db=FileProbeOut(**counts.state_db.to_dict()),
        data_dir=str(data_dir),
        dismissed=saved.dismissed,
        should_show_wizard=counts.empty and not saved.dismissed,
        stages=list(STAGES),
        last_import=saved.last_import,
        rekordbox=_detection_out(data_dir),
    )


def _live_import(store: JobStore) -> dict[str, Any] | None:
    for job in store.list(limit=200):
        if job["kind"] == SETUP_IMPORT_KIND and job["status"] in _LIVE_STATUSES:
            return job
    return None


# ----- endpoints ----------------------------------------------------------
@router.get("/status", response_model=SetupStatusOut)
def setup_status(request: Request) -> SetupStatusOut:
    """Is this a first run? Counts come from state.db, not from memory."""
    return _status_out(_data_dir(request))


@router.get("/detect/rekordbox", response_model=RekordboxDetectionOut)
def detect_rekordbox_endpoint(request: Request) -> RekordboxDetectionOut:
    """Report the rekordbox install WITHOUT opening or copying anything."""
    return _detection_out(_data_dir(request))


@router.post(
    "/import",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_import(request: Request, body: SetupImportIn) -> dict[str, Any]:
    """Queue the import as a job, so progress streams over the events bus.

    Refuses BEFORE enqueueing on anything detection can already see: a job
    that is certain to fail is worse than a 409, because it turns a clear
    refusal into a failed row somebody has to go and read.
    """
    data_dir = _data_dir(request)
    store = _store(request)

    running = _live_import(store)
    if running is not None:
        raise _refuse(
            detect.CODE_IMPORT_ALREADY_RUNNING,
            f"setup import {running['id']} is already {running['status']}; "
            "cancel it before starting another",
            status.HTTP_409_CONFLICT,
        )

    if body.source is None:
        found = detect.detect_rekordbox(data_dir)
        fatal = [
            code
            for code in found.blockers
            if code != detect.CODE_SHARE_MISSING
        ]
        if fatal:
            raise _refuse(
                fatal[0],
                _blocker_message(fatal[0], found),
                status.HTTP_409_CONFLICT,
            )

    payload: dict[str, Any] = {"data_dir": str(data_dir)}
    if body.source is not None:
        payload["source"] = body.source
    if body.limit is not None:
        payload["limit"] = body.limit

    try:
        build_argv(payload)
    except SetupPayloadError as exc:
        raise _refuse(
            "setup_payload_invalid", str(exc), status.HTTP_400_BAD_REQUEST
        ) from exc

    return store.enqueue(SETUP_IMPORT_KIND, payload)


@router.post("/dismiss", response_model=SetupStatusOut)
def dismiss(request: Request, body: SetupDismissIn) -> SetupStatusOut:
    """Skip the wizard, or re-arm it. Persisted engine-side, not in a tab."""
    data_dir = _data_dir(request)
    record.set_dismissed(data_dir, body.dismissed)
    return _status_out(data_dir)


@router.get("/stems", response_model=StemsSetupOut)
def stems_availability() -> StemsSetupOut:
    """Can a library-wide stems pass be started from setup? Honestly, no.

    The per-track separation endpoints are real and are named here. What
    does not exist is anything that runs them across a whole library, so
    the wizard's stems step renders its real choices and then says so.
    """
    from apps.stems import tiers as tiercfg

    ladder = [
        StemTierOut(
            key=tier.key,
            name=tier.name,
            where=tier.where,
            availability=tier.availability,
            unavailable_because=tier.unavailable_because,
        )
        for tier in tiercfg.ladder()
    ]
    return StemsSetupOut(
        available=False,
        reason=STEMS_UNAVAILABLE_MESSAGE,
        per_track_endpoint="/api/v1/stems/generate",
        library_scan_endpoint=None,
        tiers=ladder,
    )


#: One sentence per blocker code, so a refusal explains itself instead of
#: handing back a bare enum. A code with no entry still gets named rather
#: than swallowed -- see :func:`_blocker_message`.
_BLOCKER_MESSAGES: dict[str, Callable[[detect.RekordboxDetection], str]] = {
    detect.CODE_REKORDBOX_NOT_FOUND: lambda found: (
        "no rekordbox database was found. Looked for the live install at "
        f"{found.live_db.path} and for a working copy in the data dir."
    ),
    detect.CODE_KEY_UNAVAILABLE: lambda found: (
        f"{found.import_source} is encrypted and no SQLCipher key is "
        f"available: {found.key_detail}"
    ),
    detect.CODE_SHARE_MISSING: lambda found: (
        f"the rekordbox share dir {found.share_dir.path} is missing, so no "
        "ANLZ analysis (waveforms, beatgrids) can be resolved"
    ),
}


def _blocker_message(code: str, found: detect.RekordboxDetection) -> str:
    render = _BLOCKER_MESSAGES.get(code)
    return render(found) if render is not None else f"setup refused with {code}"


__all__ = [
    "STEMS_UNAVAILABLE_MESSAGE",
    "FileProbeOut",
    "RekordboxDetectionOut",
    "SetupDismissIn",
    "SetupImportIn",
    "SetupStatusOut",
    "StemTierOut",
    "StemsSetupOut",
    "router",
]
