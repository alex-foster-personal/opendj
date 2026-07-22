"""Relocate-files endpoints -- LANE reconcile-router (node ``relocate-files``).

Builds against the read-only reconcile contract (``routes/reconcile.py``):
this module never re-scans the whole library for the "which tracks are
broken" question, it only resolves ONE track's current recorded path
(same rekordbox-wins-over-state-layer order as ``reconcile._scan_broken``)
and finds/writes a replacement for it.

Integrator wiring (one line each, in apps/webui/server/app.py)::

    from .routes import relocate as relocate_routes
    app.include_router(relocate_routes.router, prefix=api_prefix)

Endpoints
---------

``GET /api/v1/relocate/candidates/{stable_id}`` -> :class:`RelocateCandidateList`
    Filesystem candidates for one track's dead recorded path, reusing the
    triple-validation scoring from :mod:`apps.reconcile.locate`
    (:func:`apps.reconcile.locate.find_candidates`) against
    :func:`apps.shared.audio_files.scan_music_files`. Ranked best first,
    capped at ``limit`` (default 5). Returns an EMPTY list -- never an
    error -- when the configured music roots are absent (cloud sandboxes,
    CI) or nothing scores a hit; this is a real scan of real paths, not
    mocked data, so an empty environment legitimately finds nothing.

``POST /api/v1/relocate/{stable_id}/apply`` -> :class:`RelocateApplyOut`
    Applies one candidate path as the track's new location. Requires
    ``confirm: true`` (explicit safety gate; this writes to disk-backed
    state) and that ``new_path`` exists on disk (fail fast, never patch a
    path that doesn't resolve). Two write targets, chosen by whether the
    track has a rekordbox vendor mapping (mirrors ``vendor_id`` in the
    reconcile contract):

    * vendor_id present -> patches ``djmdContent.FolderPath`` on the LIVE
      rekordbox database (:data:`apps.shared.paths.REKORDBOX_LIVE_DB`) via
      ``pyrekordbox``, with the same safety rails as the CLI
      (:mod:`apps.reconcile.apply`): refuse while Rekordbox is running,
      timestamped backup before writing, verify-by-readback after. A
      sandbox with no rekordbox install (no live db / no ``pyrekordbox``)
      gets a clean 503 -- this path is local-verify-deferred, never a
      silent no-op.
    * no vendor mapping -> patches the state-layer ``file_path`` via
      ``StateBackend.update_track`` (requires ``If-Match``, same
      optimistic-concurrency contract as ``PATCH /tracks/{stable_id}``).
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from apps.reconcile import locate
from apps.shared import audio_files, paths
from apps.shared.rekordbox_db import is_streaming_path as _rb_app_is_streaming

from .. import rb_vendor
from ..backend import StateBackend, Track
from ..deps import get_read_state, get_write_state
from ..errors import precondition_required

router = APIRouter(prefix="/relocate", tags=["relocate"])


# ----- response/request models (route-local: models.py is a hotspot file) --

class RelocateCandidateOut(BaseModel):
    path: str
    confidence: float
    signals: list[str]
    triple_validated: bool


class RelocateCandidateList(BaseModel):
    stable_id: str
    original_path: Optional[str]
    vendor_id: Optional[str]
    total: int
    candidates: list[RelocateCandidateOut]


class RelocateApplyIn(BaseModel):
    new_path: str = Field(min_length=1, description="Candidate path to adopt; must exist on disk.")
    confirm: bool = Field(
        description="Must be true. Explicit safety gate -- this writes to "
                    "the live rekordbox database or the state layer."
    )


class RelocateApplyOut(BaseModel):
    stable_id: str
    new_path: str
    target: Literal["rekordbox", "state"]
    vendor_id: Optional[str]
    backup_path: Optional[str]


# ----- shared path resolution (mirrors reconcile._scan_broken's order) -----

def _is_local(path: Optional[str]) -> bool:
    if path is None:
        return False
    if _rb_app_is_streaming(path):
        return False
    if rb_vendor.is_streaming_path(path):
        return False
    return True


def _resolve_original_path(stable_id: str, track: Track) -> tuple[Optional[str], Optional[str]]:
    """(original_path, vendor_id) for ``track``, rekordbox-wins-over-state.

    ``original_path`` is None when the track is streaming/pathless -- there
    is nothing local to relocate.
    """
    meta = rb_vendor.bulk_rb_meta([stable_id]).get(stable_id)
    folder = meta.folder_path if meta is not None else track.file_path
    vendor_id = meta.vendor_id if meta is not None else None
    if not _is_local(folder):
        return None, vendor_id
    return folder, vendor_id


# ----- candidates ------------------------------------------------------------

@router.get("/candidates/{stable_id}", response_model=RelocateCandidateList)
def get_candidates(
    stable_id: str,
    limit: int = Query(5, ge=1, le=20, description="Max ranked candidates to return"),
    backend: StateBackend = Depends(get_read_state),
) -> RelocateCandidateList:
    track = backend.get_track(stable_id)  # NotFoundError -> 404
    original_path, vendor_id = _resolve_original_path(stable_id, track)
    if original_path is None:
        return RelocateCandidateList(
            stable_id=stable_id, original_path=None, vendor_id=vendor_id,
            total=0, candidates=[],
        )

    index = locate.FsIndex.build(audio_files.scan_music_files())
    row: dict[str, str] = {
        "original_path": original_path,
        "basename": Path(original_path).name,
        "title": track.title or "",
        "artist": track.artist or "",
        "duration_s": str(track.duration_ms / 1000) if track.duration_ms else "",
        "file_size": "",
    }
    id3_cache: dict[Path, audio_files.AudioMetadata | None] = {}
    found = locate.find_candidates(row, index, id3_cache, limit=limit)
    return RelocateCandidateList(
        stable_id=stable_id, original_path=original_path, vendor_id=vendor_id,
        total=len(found),
        candidates=[
            RelocateCandidateOut(path=str(c.path), confidence=c.confidence,
                                 signals=c.signals, triple_validated=c.triple_validated)
            for c in found
        ],
    )


# ----- apply -------------------------------------------------------------

def _rekordbox_running() -> bool:
    """True if any process matches ``rekordbox`` via ``pgrep -if``."""
    try:
        r = subprocess.run(
            ["pgrep", "-if", "rekordbox"], check=False,
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _write_rekordbox_folder_path(vendor_id: str, new_path: str) -> str:
    """Patch ``djmdContent.FolderPath`` on the LIVE rekordbox db.

    Returns the backup path on success. Raises a clean ``HTTPException``
    (503) instead of a silent no-op whenever the environment cannot
    actually perform this write -- e.g. a cloud sandbox with no rekordbox
    install -- so the apply is deferred to a local verify pass rather than
    pretending to have succeeded.
    """
    if not paths.REKORDBOX_LIVE_DB.exists():
        raise HTTPException(status_code=503, detail={
            "code": "REKORDBOX_DB_UNAVAILABLE",
            "message": (
                f"no live rekordbox database at {paths.REKORDBOX_LIVE_DB}; "
                "apply this relocate on a machine with rekordbox installed"
            ),
        })
    try:
        from pyrekordbox import Rekordbox6Database  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "PYREKORDBOX_UNAVAILABLE",
            "message": "pyrekordbox is not installed in this environment",
        }) from exc

    if _rekordbox_running():
        raise HTTPException(status_code=409, detail={
            "code": "REKORDBOX_RUNNING",
            "message": "Rekordbox is currently running; quit it before relocating files",
        })

    backup_dir = paths.DATA_DIR / "reconcile" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = backup_dir / f"master.{ts}.db"
    shutil.copy2(paths.REKORDBOX_LIVE_DB, backup)

    db = Rekordbox6Database(path=str(paths.REKORDBOX_LIVE_DB))
    try:
        content = db.get_content(ID=vendor_id)
        if content is None:
            raise HTTPException(status_code=422, detail={
                "code": "VENDOR_ID_NOT_FOUND",
                "message": f"rekordbox djmdContent ID {vendor_id} not found in the live db",
            })
        content.FolderPath = new_path
        db.commit()
    finally:
        db.close()

    verify_db = Rekordbox6Database(path=str(paths.REKORDBOX_LIVE_DB))
    try:
        readback = verify_db.get_content(ID=vendor_id)
        if readback is None or readback.FolderPath != new_path:
            raise HTTPException(status_code=500, detail={
                "code": "RELOCATE_VERIFY_FAILED",
                "message": (
                    f"FolderPath did not read back as {new_path!r} after write; "
                    f"restore from backup if needed: {backup}"
                ),
            })
    finally:
        verify_db.close()

    return str(backup)


@router.post("/{stable_id}/apply", response_model=RelocateApplyOut)
def apply_relocate(
    stable_id: str,
    body: RelocateApplyIn,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    backend: StateBackend = Depends(get_write_state),
):
    if not body.confirm:
        raise HTTPException(status_code=422, detail={
            "code": "CONFIRM_REQUIRED",
            "message": "confirm must be true to apply a relocate",
        })

    new_path = Path(body.new_path)
    if not new_path.exists():
        raise HTTPException(status_code=422, detail={
            "code": "CANDIDATE_PATH_NOT_FOUND",
            "message": f"candidate path does not exist on disk: {new_path}",
        })

    backend.get_track(stable_id)  # NotFoundError -> 404
    meta = rb_vendor.bulk_rb_meta([stable_id]).get(stable_id)
    vendor_id = meta.vendor_id if meta is not None else None

    if vendor_id is not None:
        backup_path = _write_rekordbox_folder_path(vendor_id, str(new_path))
        return RelocateApplyOut(
            stable_id=stable_id, new_path=str(new_path), target="rekordbox",
            vendor_id=vendor_id, backup_path=backup_path,
        )

    if not if_match:
        return precondition_required(
            "POST /relocate/{stable_id}/apply requires If-Match when the "
            "track has no rekordbox vendor mapping (state-layer file_path write)"
        )
    backend.update_track(
        stable_id, {"file_path": str(new_path)}, expected_etag=if_match, source="webui",
    )
    return RelocateApplyOut(
        stable_id=stable_id, new_path=str(new_path), target="state",
        vendor_id=None, backup_path=None,
    )


__all__ = [
    "RelocateApplyIn",
    "RelocateApplyOut",
    "RelocateCandidateList",
    "RelocateCandidateOut",
    "router",
]
