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

import os
import shutil
import subprocess
import threading
import uuid
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
from ..etag import compute_etag, strip_quotes

router = APIRouter(prefix="/relocate", tags=["relocate"])
_REKORDBOX_WRITE_LOCK = threading.Lock()


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
    expected_original_path: str = Field(
        min_length=1,
        description="Recorded path returned with the selected candidate; prevents stale targeting.",
    )
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


def _require_current_etag(track: Track, if_match: str | None) -> None:
    """Reject a relocate based on a stale track view before selecting a target."""
    if not if_match:
        raise HTTPException(status_code=428, detail={
            "code": "PRECONDITION_REQUIRED",
            "message": "POST /relocate/{stable_id}/apply requires If-Match",
        })
    current = compute_etag(track.stable_id, track.updated_at)
    if strip_quotes(current) != strip_quotes(if_match):
        raise HTTPException(status_code=409, detail={
            "code": "RELOCATE_ETAG_CONFLICT",
            "message": "track changed since candidates were selected; refresh and choose again",
        })


def _validated_candidate_path(new_path: str) -> str:
    """Return a canonical regular audio path contained in a configured root.

    ``resolve`` prevents a symlink from escaping a music root and the
    descriptor check rejects a final-component symlink swap between stat and
    use. We persist the canonical path, never the caller's spelling.
    """
    requested = Path(new_path)
    if not requested.is_absolute():
        raise HTTPException(status_code=422, detail={
            "code": "CANDIDATE_PATH_NOT_ABSOLUTE",
            "message": "candidate path must be absolute",
        })
    try:
        before = requested.lstat()
        resolved = requested.resolve(strict=True)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise HTTPException(status_code=503, detail={
                "code": "CANDIDATE_PATH_GUARD_UNAVAILABLE",
                "message": "this platform cannot safely verify a symlink-free candidate path",
            })
        descriptor = os.open(str(resolved), os.O_RDONLY | no_follow)
        try:
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "CANDIDATE_PATH_NOT_FOUND",
            "message": f"candidate path does not exist on disk: {requested}",
        }) from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "CANDIDATE_PATH_UNSAFE",
            "message": f"candidate path must be a non-symlink readable file: {requested}",
        }) from exc
    if requested.is_symlink() or before.st_ino != after.st_ino or not resolved.is_file():
        raise HTTPException(status_code=422, detail={
            "code": "CANDIDATE_PATH_UNSAFE",
            "message": f"candidate path must be a stable regular file: {requested}",
        })
    for root in paths.MUSIC_ROOTS:
        try:
            resolved.relative_to(root.resolve(strict=True))
            return str(resolved)
        except (FileNotFoundError, ValueError):
            continue
    raise HTTPException(status_code=422, detail={
        "code": "CANDIDATE_PATH_OUTSIDE_MUSIC_ROOTS",
        "message": "candidate path must be contained in a configured music root",
    })


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

def _assert_rekordbox_not_running() -> None:
    """Fail closed unless the Rekordbox process guard is available and clear."""
    try:
        r = subprocess.run(
            ["pgrep", "-if", "rekordbox"], check=False,
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail={
            "code": "REKORDBOX_PROCESS_CHECK_UNAVAILABLE",
            "message": "cannot verify that Rekordbox is stopped",
        })
    if r.returncode not in (0, 1):
        raise HTTPException(status_code=503, detail={
            "code": "REKORDBOX_PROCESS_CHECK_FAILED",
            "message": "cannot verify that Rekordbox is stopped",
        })
    if r.returncode == 0 and r.stdout.strip():
        raise HTTPException(status_code=409, detail={
            "code": "REKORDBOX_RUNNING",
            "message": "Rekordbox is currently running; quit it before relocating files",
        })


def _backup_live_database() -> Path:
    backup_dir = paths.DATA_DIR / "reconcile" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup = backup_dir / f"master.{stamp}.{uuid.uuid4().hex}.db"
    shutil.copy2(paths.REKORDBOX_LIVE_DB, backup)
    if backup.stat().st_size != paths.REKORDBOX_LIVE_DB.stat().st_size:
        raise HTTPException(status_code=500, detail={
            "code": "REKORDBOX_BACKUP_FAILED",
            "message": f"backup size verification failed: {backup}",
        })
    return backup


def _restore_live_database(backup: Path) -> None:
    """Restore a verified backup only while the Rekordbox process guard is clear."""
    _assert_rekordbox_not_running()
    shutil.copy2(backup, paths.REKORDBOX_LIVE_DB)
    if backup.stat().st_size != paths.REKORDBOX_LIVE_DB.stat().st_size:
        raise HTTPException(status_code=500, detail={
            "code": "REKORDBOX_ROLLBACK_FAILED",
            "message": f"backup restore size verification failed: {backup}",
        })


def _write_rekordbox_folder_path(
    vendor_id: str, expected_original_path: str, new_path: str,
) -> str:
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

    with _REKORDBOX_WRITE_LOCK:
        _assert_rekordbox_not_running()
        db = Rekordbox6Database(path=str(paths.REKORDBOX_LIVE_DB))
        backup: Path | None = None
        write_started = False
        write_error: Exception | None = None
        try:
            content = db.get_content(ID=vendor_id)
            if content is None:
                raise HTTPException(status_code=422, detail={
                    "code": "VENDOR_ID_NOT_FOUND",
                    "message": f"rekordbox djmdContent ID {vendor_id} not found in the live db",
                })
            if content.FolderPath != expected_original_path:
                raise HTTPException(status_code=409, detail={
                    "code": "RELOCATE_TARGET_CHANGED",
                    "message": "live rekordbox FolderPath changed; refresh candidates before applying",
                })
            backup = _backup_live_database()
            _assert_rekordbox_not_running()
            write_started = True
            content.FolderPath = new_path
            db.commit()
        except Exception as exc:
            write_error = exc
        finally:
            db.close()

        if write_error is not None:
            if backup is None or not write_started:
                raise write_error
            _restore_live_database(backup)
            raise HTTPException(status_code=500, detail={
                "code": "RELOCATE_WRITE_ROLLED_BACK",
                "message": f"live write failed and backup was restored: {backup}",
            }) from write_error

        assert backup is not None
        verify_db = Rekordbox6Database(path=str(paths.REKORDBOX_LIVE_DB))
        verify_failed = False
        try:
            readback = verify_db.get_content(ID=vendor_id)
            verify_failed = readback is None or readback.FolderPath != new_path
        finally:
            verify_db.close()
        if verify_failed:
            _restore_live_database(backup)
            raise HTTPException(status_code=500, detail={
                "code": "RELOCATE_WRITE_ROLLED_BACK",
                "message": f"FolderPath verification failed and backup was restored: {backup}",
            })
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

    track = backend.get_track(stable_id)  # NotFoundError -> 404
    _require_current_etag(track, if_match)
    original_path, vendor_id = _resolve_original_path(stable_id, track)
    if original_path is None or original_path != body.expected_original_path:
        raise HTTPException(status_code=409, detail={
            "code": "RELOCATE_TARGET_CHANGED",
            "message": "recorded path changed; refresh candidates before applying",
        })
    new_path = _validated_candidate_path(body.new_path)
    index = locate.FsIndex.build(audio_files.scan_music_files())
    row = {
        "original_path": original_path,
        "basename": Path(original_path).name,
        "title": track.title or "",
        "artist": track.artist or "",
        "duration_s": str(track.duration_ms / 1000) if track.duration_ms else "",
        "file_size": "",
    }
    current_candidates = locate.find_candidates(row, index, {}, limit=20)
    if new_path not in {str(candidate.path.resolve()) for candidate in current_candidates}:
        raise HTTPException(status_code=409, detail={
            "code": "RELOCATE_CANDIDATE_STALE",
            "message": "candidate no longer matches this track; refresh candidates before applying",
        })

    if vendor_id is not None:
        backup_path = _write_rekordbox_folder_path(
            vendor_id, original_path, new_path,
        )
        return RelocateApplyOut(
            stable_id=stable_id, new_path=str(new_path), target="rekordbox",
            vendor_id=vendor_id, backup_path=backup_path,
        )

    updated = backend.update_track(
        stable_id, {"file_path": new_path}, expected_etag=if_match, source="webui",
    )
    if updated.file_path != new_path or backend.get_track(stable_id).file_path != new_path:
        raise HTTPException(status_code=500, detail={
            "code": "RELOCATE_STATE_READBACK_FAILED",
            "message": "state-layer file_path did not read back after update",
        })
    return RelocateApplyOut(
        stable_id=stable_id, new_path=new_path, target="state",
        vendor_id=None, backup_path=None,
    )


__all__ = [
    "RelocateApplyIn",
    "RelocateApplyOut",
    "RelocateCandidateList",
    "RelocateCandidateOut",
    "router",
]
