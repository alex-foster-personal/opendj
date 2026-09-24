"""First-run setup HTTP surface -- one endpoint per wizard step.

AGENT-NATIVE PARITY is the hard constraint here: every step the wizard walks
a human through is a request an agent can make on its own, in the same
order, with the same refusals. There is no browser-only state.

    GET  /api/v1/setup/status            is the library empty, what is here,
                                         and is this a developer checkout
    GET  /api/v1/setup/detect/rekordbox  what is installed, real paths
    POST /api/v1/setup/import            run the import AS A JOB
    POST /api/v1/setup/dismiss           skip (or un-skip) the wizard
    GET  /api/v1/setup/stems             can stems analysis run at all,
                                         read off the job registry

Refusals carry ``{"code", "message"}`` so a caller can branch on the code
instead of pattern-matching prose. The codes are the ones in
:mod:`apps.engine_core.setup.detect`, and they stay distinct: a missing
rekordbox, an unreachable key and a failed decrypt are three problems.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ValidationError

from apps.engine_core.build_info import (
    BUILD_IDENTITY_STATE_ATTR,
    CODE_BUILD_IDENTITY_UNAVAILABLE,
    BuildIdentity,
    BuildInfoUnavailable,
)
from apps.engine_core.jobs.api import JobOut
from apps.engine_core.jobs.runner import known_kinds
from apps.engine_core.jobs.store import JobStore
from apps.engine_core.setup import detect, record
from apps.engine_core.setup.importer import FOLDER_STAGES, STAGES
from apps.engine_core.setup.jobs import (
    SETUP_IMPORT_KIND,
    SetupPayloadError,
    build_argv,
)
from apps.engine_core.setup.schemas import (
    AccessProbeOut,
    FileProbeOut,
    FolderCandidatesOut,
    FolderImportIn,
    FolderLastImportOut,
    FolderScanOut,
    LastImportOut,
    PermissionsOut,
    RekordboxDetectionOut,
    SetupDismissIn,
    SetupImportIn,
    SetupStatusOut,
    StemsSetupOut,
    StemTierOut,
    normalize_setup_folder_path,
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

#: The other half of the same honesty. Once af--stems-modal's job kind is
#: registered, saying "not yet available" would be the lie, so the verdict
#: reads off the registry and picks the sentence that is true.
STEMS_READY_MESSAGE: str = (
    "stems separation is wired; ask /api/v1/stems/plan for the batch size "
    "and cost before enqueueing"
)

#: How many example paths /detect/folder returns. Enough to recognise the
#: folder, small enough that a 40,000-file library is not serialised into a
#: response nobody asked for.
SCAN_SAMPLE_SIZE: int = 5


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


def _build_identity(request: Request) -> BuildIdentity:
    identity = getattr(request.app.state, BUILD_IDENTITY_STATE_ATTR, None)
    if identity is None:
        raise RuntimeError(
            f"build identity is not mounted on app.state."
            f"{BUILD_IDENTITY_STATE_ATTR}; setup cannot tell a developer "
            "checkout from an installed build, and it will not guess. "
            "add_build_info_route mounts it."
        )
    return identity


def _refuse(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(
        status_code=http_status, detail={"code": code, "message": message}
    )


def _detection_out(data_dir: Path) -> RekordboxDetectionOut:
    found = detect.detect_rekordbox(data_dir)
    return RekordboxDetectionOut(
        **found.to_dict(), rekordbox_running=detect.rekordbox_is_running()
    )


def _permissions_out() -> PermissionsOut:
    probes = detect.music_root_access()
    denied = detect.denied_roots(probes)
    return PermissionsOut(
        all_readable=not denied,
        denied=denied,
        roots=[AccessProbeOut(**probe.to_dict()) for probe in probes],
        how_to_grant=detect.GRANT_INSTRUCTIONS,
    )


_LAST_IMPORT_MODELS: dict[str, type[BaseModel]] = {
    "rekordbox": LastImportOut,
    "folder": FolderLastImportOut,
}


def _last_import_out(
    saved: record.SetupRecord,
) -> LastImportOut | FolderLastImportOut | None:
    """Validate the stored outcome into its wire model, or fail loudly.

    Dispatched on the record's own ``kind`` rather than on which keys happen
    to be present: guessing the shape is how a folder import ends up
    rendered as a rekordbox one with its numbers silently missing.

    ``record.read`` already refuses a record it cannot parse, so a dict that
    does not fit its declared model means an outcome was written by a
    different engine version without bumping RECORD_VERSION -- a bug worth a
    500 rather than a "done" screen quietly missing its numbers.
    """
    if saved.last_import is None:
        return None
    kind = saved.last_import.get("kind")
    model = _LAST_IMPORT_MODELS.get(str(kind))
    if model is None:
        raise record.SetupRecordError(
            f"the stored import outcome declares kind {kind!r}; this engine "
            f"knows {sorted(_LAST_IMPORT_MODELS)}"
        )
    try:
        return model(**saved.last_import)  # type: ignore[return-value]
    except ValidationError as exc:
        raise record.SetupRecordError(
            f"the stored {kind} import outcome does not match this engine's "
            f"{model.__name__} model: {exc}"
        ) from exc


def _status_out(data_dir: Path, dev_mode: bool) -> SetupStatusOut:
    counts = detect.library_counts(data_dir)
    saved = record.read(data_dir)
    return SetupStatusOut(
        library_empty=counts.empty,
        tracks=counts.tracks,
        playlists=counts.playlists,
        state_db=FileProbeOut(**counts.state_db.to_dict()),
        data_dir=str(data_dir),
        dismissed=saved.dismissed,
        dev_mode=dev_mode,
        should_show_wizard=(
            counts.empty and not saved.dismissed and not dev_mode
        ),
        stages=list(STAGES),
        folder_stages=list(FOLDER_STAGES),
        last_import=_last_import_out(saved),
        rekordbox=_detection_out(data_dir),
        permissions=_permissions_out(),
    )


def _status(request: Request) -> SetupStatusOut:
    """The status model, with the wizard gate resolved against the build.

    THE DEV-MODE RULE lives here, once, so /status and /dismiss cannot
    disagree: an engine running out of a checkout (``source == "repo"``)
    never auto-triggers the wizard. A developer boots with a throwaway
    ``--data-dir`` all day, and an empty library is that dir being new, not
    a first run in need of an import.

    An identity that could not be resolved refuses under the SAME code
    /api/v1/build-info uses. Picking a mode anyway would mean an installed
    build with a broken manifest silently behaving like a checkout, which
    is precisely the wizard-never-appears bug this flag exists to make
    legible.
    """
    try:
        source = _build_identity(request).require().source
    except BuildInfoUnavailable as exc:
        raise _refuse(
            CODE_BUILD_IDENTITY_UNAVAILABLE,
            str(exc),
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    return _status_out(_data_dir(request), dev_mode=source == "repo")


def _live_import(store: JobStore) -> dict[str, Any] | None:
    for job in store.list(limit=200):
        if job["kind"] == SETUP_IMPORT_KIND and job["status"] in _LIVE_STATUSES:
            return job
    return None


# ----- endpoints ----------------------------------------------------------
@router.get(
    "/status",
    response_model=SetupStatusOut,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "this build cannot state its own identity, so the "
            "wizard gate cannot be decided"
        }
    },
)
def setup_status(request: Request) -> SetupStatusOut:
    """Is this a first run? Counts come from state.db, not from memory."""
    return _status(request)


@router.get("/detect/rekordbox", response_model=RekordboxDetectionOut)
def detect_rekordbox_endpoint(request: Request) -> RekordboxDetectionOut:
    """Report the rekordbox install WITHOUT opening or copying anything."""
    return _detection_out(_data_dir(request))


@router.get("/permissions", response_model=PermissionsOut)
def permissions() -> PermissionsOut:
    """Can this process read the folders the music lives in?

    Reported, never refused on. macOS answers a blocked listing with an
    EMPTY one rather than an error, which is how a library with 40,000
    tracks in it becomes a library with none and nobody is told. Every
    count this API reports afterwards has to be read against the ``denied``
    list here: zero files in a folder we were not allowed to open is not
    zero files.
    """
    return _permissions_out()


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
    if body.refresh_decrypt:
        payload["refresh_decrypt"] = True

    try:
        build_argv(payload)
    except SetupPayloadError as exc:
        raise _refuse(
            "setup_payload_invalid", str(exc), status.HTTP_400_BAD_REQUEST
        ) from exc

    return store.enqueue(SETUP_IMPORT_KIND, payload)


@router.get("/detect/music-folders", response_model=FolderCandidatesOut)
def music_folder_candidates() -> FolderCandidatesOut:
    """Existing folders under HOME worth suggesting, before typing a path.

    Never a guess: only paths that exist on this machine are returned, and
    a candidate macOS refuses to list is still reported, marked denied,
    rather than silently omitted.
    """
    return FolderCandidatesOut(
        candidates=[
            AccessProbeOut(**probe.to_dict())
            for probe in detect.music_folder_candidates()
        ]
    )


@router.get("/detect/folder", response_model=FolderScanOut)
def detect_folder(
    path: Annotated[
        str, Query(min_length=1, description="absolute folder path to inspect")
    ],
) -> FolderScanOut:
    """Look inside a candidate folder WITHOUT importing it.

    This is the no-rekordbox branch's version of the detect step. The count
    it returns is only meaningful when ``denied`` is false: macOS answers a
    blocked listing with an empty one, so "0 audio files" from a denied
    folder would be a fabrication.
    """
    from apps.shared import fs_access

    canon = normalize_setup_folder_path(path)
    target = Path(canon)
    probe = fs_access.probe_readable(target)
    audio = 0
    placeholders = 0
    sample: list[str] = []
    if probe.readable:
        from apps.shared.state.ingest import folder as folder_ingest

        found, _denied, placeholders = folder_ingest.collect_audio([target])
        audio = len(found)
        sample = [str(entry.path) for entry in found[:SCAN_SAMPLE_SIZE]]
    return FolderScanOut(
        path=canon,
        exists=probe.exists,
        readable=probe.readable,
        denied=probe.denied,
        detail=probe.detail,
        audio_files=audio,
        icloud_placeholders=placeholders,
        how_to_grant=fs_access.GRANT_INSTRUCTIONS,
        sample=sample,
    )


@router.post(
    "/import/folder",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_folder_import(request: Request, body: FolderImportIn) -> dict[str, Any]:
    """Queue a folder import: tags only, no analysis, and it says so.

    Its own endpoint rather than a mode flag on /import, because its
    refusals are different ones: a folder can be absent, unreadable, or
    genuinely empty, and none of those is a rekordbox problem.
    """
    from apps.shared import fs_access

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

    probes = fs_access.probe_all([Path(folder) for folder in body.folders])
    if not any(probe.readable for probe in probes):
        denied = fs_access.denied_roots(probes)
        if denied:
            raise _refuse(
                detect.CODE_ACCESS_DENIED,
                f"macOS refused to list {', '.join(denied)}. "
                f"{fs_access.GRANT_INSTRUCTIONS}",
                status.HTTP_403_FORBIDDEN,
            )
        raise _refuse(
            detect.CODE_REKORDBOX_NOT_FOUND,
            "none of "
            f"{', '.join(probe.path for probe in probes)} is a readable "
            "folder: "
            + "; ".join(f"{probe.path} {probe.detail}" for probe in probes),
            status.HTTP_409_CONFLICT,
        )

    payload: dict[str, Any] = {
        "data_dir": str(data_dir),
        "mode": "folder",
        "roots": body.folders,
    }
    if body.limit is not None:
        payload["limit"] = body.limit

    try:
        build_argv(payload)
    except SetupPayloadError as exc:
        raise _refuse(
            "setup_payload_invalid", str(exc), status.HTTP_400_BAD_REQUEST
        ) from exc

    return store.enqueue(SETUP_IMPORT_KIND, payload)


@router.post(
    "/dismiss",
    response_model=SetupStatusOut,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "this build cannot state its own identity, so the "
            "wizard gate cannot be decided"
        }
    },
)
def dismiss(request: Request, body: SetupDismissIn) -> SetupStatusOut:
    """Skip the wizard, or re-arm it. Persisted engine-side, not in a tab."""
    record.set_dismissed(_data_dir(request), body.dismissed)
    return _status(request)


@router.get("/stems", response_model=StemsSetupOut)
def stems_availability() -> StemsSetupOut:
    """Can a library-wide stems pass be started from setup?

    ANSWERED FROM EVIDENCE, never from a constant. The verdict is whether
    this engine actually has a worker registered for ``stems.separate``: a
    legacy boot, or a chassis whose composition root never wired the kind,
    genuinely cannot run one and says so in the tester's own words.

    The tier ladder is real either way. An unavailable rung carries the
    reason it is unavailable, so the wizard renders true choices rather
    than a placeholder list.
    """
    from apps.stems import job as stems_job
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
    registered = stems_job.JOB_KIND in known_kinds()
    return StemsSetupOut(
        available=registered,
        reason=STEMS_READY_MESSAGE if registered else STEMS_UNAVAILABLE_MESSAGE,
        job_kind=stems_job.JOB_KIND,
        plan_endpoint="/api/v1/stems/plan",
        enqueue_endpoint="/api/v1/jobs",
        per_track_endpoint="/api/v1/stems/generate",
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


#: Re-exported so ``from ...setup.api import SetupStatusOut`` keeps working
#: for callers that were written before the models moved to
#: :mod:`apps.engine_core.setup.schemas`. The models themselves live there.
__all__ = [
    "STEMS_READY_MESSAGE",
    "STEMS_UNAVAILABLE_MESSAGE",
    "AccessProbeOut",
    "FileProbeOut",
    "FolderCandidatesOut",
    "FolderImportIn",
    "FolderLastImportOut",
    "FolderScanOut",
    "LastImportOut",
    "PermissionsOut",
    "RekordboxDetectionOut",
    "SetupDismissIn",
    "SetupImportIn",
    "SetupStatusOut",
    "StemTierOut",
    "StemsSetupOut",
    "router",
]
