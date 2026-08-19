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
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, ValidationError

from apps.engine_core.jobs.api import JobOut
from apps.engine_core.jobs.store import JobStore
from apps.engine_core.setup import detect, record
from apps.engine_core.setup.importer import FOLDER_STAGES, STAGES
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

#: How many example paths /detect/folder returns. Enough to recognise the
#: folder, small enough that a 40,000-file library is not serialised into a
#: response nobody asked for.
SCAN_SAMPLE_SIZE: int = 5


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


class AccessProbeOut(BaseModel):
    """One folder, and whether this process can actually read it.

    ``exists`` true with ``readable`` false and ``denied`` true is the macOS
    TCC case: the folder is there and full of music, and the listing is
    refused, so anything that counted files inside it would report zero.
    """

    path: str
    exists: bool
    readable: bool
    denied: bool
    detail: str


class PermissionsOut(BaseModel):
    """The folder-access answer, and what to do about a refusal."""

    all_readable: bool
    denied: list[str] = Field(default_factory=list)
    roots: list[AccessProbeOut] = Field(default_factory=list)
    how_to_grant: str


class LastImportOut(BaseModel):
    """What the previous rekordbox import did. Mirrors ``ImportOutcome``.

    Typed rather than a free-form object: a caller reading a track count off
    an untyped dict has no contract, and the wizard's "done" screen is built
    entirely out of these numbers.
    """

    kind: Literal["rekordbox"] = "rekordbox"
    started_at: str
    finished_at: str
    source: str
    source_was_encrypted: bool
    ingested_from: str
    tracks: int
    playlists: int
    analyses_linked: int
    analyses_expected: int
    rekordbox_tracks: int
    share_root: str
    rekordbox_was_running: bool
    #: Present on records written by this engine version. Older records have
    #: no such field, and defaulting it to [] would claim "nothing was
    #: denied" about a run that never asked -- so the wizard checks
    #: `permissions` for the live answer and treats this as history only.
    unreadable_music_roots: list[str] = Field(default_factory=list)


class FolderLastImportOut(BaseModel):
    """What the previous FOLDER import did. Mirrors ``FolderImportOutcome``.

    A different model rather than optional fields on the rekordbox one,
    because the two describe different work: there is no decrypt here, no
    playlists, and -- the field that matters --
    ``tracks_without_analysis``, which equals the tracks written.
    """

    kind: Literal["folder"] = "folder"
    started_at: str
    finished_at: str
    roots: list[str] = Field(default_factory=list)
    unreadable_roots: list[str] = Field(default_factory=list)
    files_seen: int
    files_dataless: int
    files_without_tags: int
    tracks: int
    tracks_written: int
    tracks_without_analysis: int
    analysis_available: bool = False
    analysis_detail: str


class SetupStatusOut(BaseModel):
    """Everything the wizard needs to decide whether to show itself."""

    library_empty: bool
    tracks: int
    playlists: int
    state_db: FileProbeOut
    data_dir: str
    dismissed: bool
    should_show_wizard: bool
    #: The rekordbox import's stages, in order.
    stages: list[str]
    #: The folder import's stages. Shorter on purpose: no snapshot and no
    #: decrypt, because there is no rekordbox database in that path.
    folder_stages: list[str]
    last_import: LastImportOut | FolderLastImportOut | None = Field(
        default=None, discriminator="kind"
    )
    rekordbox: RekordboxDetectionOut
    #: Folder access, inlined so the wizard's first render already knows
    #: whether a count of zero means "empty" or "not allowed to look".
    permissions: PermissionsOut


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
    refresh_decrypt: bool = Field(
        default=False,
        description=(
            "re-decrypt the encrypted snapshot instead of reusing an "
            "existing master.plain.db; the wizard's half of the ingest-rb "
            "CLI's --refresh-decrypt"
        ),
    )


class FolderImportIn(BaseModel):
    """Point at one or more folders of audio files. No rekordbox involved."""

    folders: list[str] = Field(
        min_length=1,
        description="absolute paths to walk; at least one",
    )
    limit: int | None = Field(
        default=None, ge=1, description="import at most N files"
    )


class FolderScanOut(BaseModel):
    """What a candidate folder actually holds, before anything is imported.

    ``audio_files`` counts what could be READ. When ``denied`` is true that
    number is not a count of the folder, it is a count of nothing, and
    ``detail`` says so -- which is the difference between "this folder is
    empty" and "macOS would not let me look".
    """

    path: str
    exists: bool
    readable: bool
    denied: bool
    detail: str
    audio_files: int
    icloud_placeholders: int
    how_to_grant: str
    sample: list[str] = Field(default_factory=list)


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
        folder_stages=list(FOLDER_STAGES),
        last_import=_last_import_out(saved),
        rekordbox=_detection_out(data_dir),
        permissions=_permissions_out(),
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

    target = Path(path).expanduser()
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
        path=str(target),
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
    "AccessProbeOut",
    "FileProbeOut",
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
