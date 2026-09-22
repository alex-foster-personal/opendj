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
import stat
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from apps.reconcile import locate
from apps.shared import audio_files, fs_residency, paths
from apps.shared.events import publish
from apps.shared.rekordbox_db import is_streaming_path as _rb_app_is_streaming
from apps.shared.rekordbox_writeback import require_writeback_enabled

from .. import rb_vendor
from ..backend import StateBackend, Track
from ..deps import get_read_state, get_write_state
from ..etag import compute_etag, strip_quotes

router = APIRouter(prefix="/relocate", tags=["relocate"])
_REKORDBOX_WRITE_LOCK = threading.Lock()
_MutationResult = TypeVar("_MutationResult")


# ----- response/request models (route-local: models.py is a hotspot file) --

class RelocateCandidateOut(BaseModel):
    path: str
    identity_token: str
    confidence: float
    signals: list[str]
    triple_validated: bool


class RelocateCandidateList(BaseModel):
    stable_id: str
    original_path: str | None
    vendor_id: str | None
    total: int
    candidates: list[RelocateCandidateOut]


class RelocateApplyIn(BaseModel):
    new_path: str = Field(min_length=1, description="Candidate path to adopt; must exist on disk.")
    expected_candidate_identity: str = Field(
        min_length=1,
        description="Opaque identity token returned for the selected candidate.",
    )
    expected_original_path: str = Field(
        min_length=1,
        description="Recorded path returned with the selected candidate; prevents stale targeting.",
    )
    expected_vendor_id: str | None = Field(
        description="Vendor mapping returned with the selected candidate, or null for state writes.",
    )
    confirm: bool = Field(
        description="Must be true. Explicit safety gate -- this writes to "
                    "the live rekordbox database or the state layer."
    )


class RelocateApplyOut(BaseModel):
    stable_id: str
    new_path: str
    target: Literal["rekordbox", "state"]
    vendor_id: str | None
    backup_path: str | None


@dataclass(frozen=True, slots=True)
class CandidateFile:
    """Canonical candidate path plus its stable filesystem identity."""

    path: str
    identity_token: str


# ----- shared path resolution (mirrors reconcile._scan_broken's order) -----

def _is_local(path: str | None) -> bool:
    if path is None:
        return False
    if _rb_app_is_streaming(path):
        return False
    if rb_vendor.is_streaming_path(path):
        return False
    return True


def _resolve_original_path(stable_id: str, track: Track) -> tuple[str | None, str | None]:
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
    current = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    if strip_quotes(current) != strip_quotes(if_match):
        raise HTTPException(status_code=409, detail={
            "code": "RELOCATE_ETAG_CONFLICT",
            "message": "track changed since candidates were selected; refresh and choose again",
        })


def _identity_token(stat_result: os.stat_result) -> str:
    """Opaque stable identity for the candidate selected by the user."""
    return ":".join(
        str(value)
        for value in (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )
    )


def _open_candidate_file(
    new_path: str, expected_identity: str | None = None,
) -> tuple[CandidateFile, int]:
    """Open a canonical candidate without following its final symlink.

    Callers must keep the descriptor open until their mutation boundary. The
    same token is checked on the selection read and immediately before the
    mutation, so delete, rename, and symlink replacement races fail closed.
    """
    requested = Path(new_path)
    if not requested.is_absolute():
        raise HTTPException(status_code=422, detail={
            "code": "CANDIDATE_PATH_NOT_ABSOLUTE",
            "message": "candidate path must be absolute",
        })
    # Stat before open: opening a dataless stub can trigger iCloud materialise.
    if not fs_residency.is_materialised(requested):
        raise HTTPException(status_code=422, detail={
            "code": "CANDIDATE_PATH_NOT_MATERIALISED",
            "message": (
                "candidate path is missing or not materialised "
                f"(dataless/iCloud stub): {requested}"
            ),
        })
    descriptor: int | None = None
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
        after = os.fstat(descriptor)
        if (
            requested.is_symlink()
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or not stat.S_ISREG(after.st_mode)
            or fs_residency.is_dataless_stub(after)
        ):
            raise HTTPException(status_code=422, detail={
                "code": "CANDIDATE_PATH_UNSAFE",
                "message": f"candidate path must be a stable regular file: {requested}",
            })
        for root in paths.MUSIC_ROOTS:
            try:
                resolved.relative_to(root.resolve(strict=True))
                break
            except (FileNotFoundError, ValueError):
                continue
        else:
            raise HTTPException(status_code=422, detail={
                "code": "CANDIDATE_PATH_OUTSIDE_MUSIC_ROOTS",
                "message": "candidate path must be contained in a configured music root",
            })
        candidate = CandidateFile(str(resolved), _identity_token(after))
        if expected_identity is not None and candidate.identity_token != expected_identity:
            raise HTTPException(status_code=409, detail={
                "code": "RELOCATE_CANDIDATE_CHANGED",
                "message": "candidate changed since it was selected; refresh candidates",
            })
        owned_descriptor = descriptor
        descriptor = None
        return candidate, owned_descriptor
    except HTTPException:
        raise
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
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _assert_candidate_path_identity(
    candidate: CandidateFile, descriptor: int, expected_identity: str,
) -> None:
    """Ensure the pathname still names the exact file held by ``descriptor``."""
    try:
        path_stat = Path(candidate.path).lstat()
        descriptor_stat = os.fstat(descriptor)
    except OSError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "RELOCATE_CANDIDATE_CHANGED",
            "message": "candidate changed during apply; no location was persisted",
        }) from exc
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or (path_stat.st_dev, path_stat.st_ino)
        != (descriptor_stat.st_dev, descriptor_stat.st_ino)
        or _identity_token(path_stat) != expected_identity
        or _identity_token(descriptor_stat) != expected_identity
    ):
        raise HTTPException(status_code=409, detail={
            "code": "RELOCATE_CANDIDATE_CHANGED",
            "message": "candidate changed during apply; no location was persisted",
        })


def _validated_candidate_path(new_path: str) -> CandidateFile:
    """Validate a candidate now, then close its descriptor before later checks."""
    candidate, descriptor = _open_candidate_file(new_path)
    os.close(descriptor)
    return candidate


def _mutate_with_candidate_guard(
    candidate: CandidateFile,
    expected_identity: str,
    mutation: Callable[[CandidateFile, Callable[[], None]], _MutationResult],
) -> _MutationResult:
    """Hold the selected file while a mutation brackets itself with identity checks."""
    checked, descriptor = _open_candidate_file(candidate.path, expected_identity)
    try:

        def guard() -> None:
            _assert_candidate_path_identity(checked, descriptor, expected_identity)

        return mutation(checked, guard)
    finally:
        os.close(descriptor)


# ----- candidates ------------------------------------------------------------

@router.get("/candidates/{stable_id}", response_model=RelocateCandidateList)
def get_candidates(
    stable_id: str,
    limit: int = Query(5, ge=1, le=20, description="Max ranked candidates to return"),
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
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
    candidates: list[RelocateCandidateOut] = []
    for found_candidate in found:
        try:
            candidate = _validated_candidate_path(str(found_candidate.path))
        except HTTPException:
            continue
        candidates.append(RelocateCandidateOut(
            path=candidate.path,
            identity_token=candidate.identity_token,
            confidence=found_candidate.confidence,
            signals=found_candidate.signals,
            triple_validated=found_candidate.triple_validated,
        ))
    return RelocateCandidateList(
        stable_id=stable_id, original_path=original_path, vendor_id=vendor_id,
        total=len(candidates), candidates=candidates,
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
    vendor_id: str,
    expected_original_path: str,
    candidate: CandidateFile,
    expected_identity: str,
) -> str:
    """Patch ``djmdContent.FolderPath`` on the LIVE rekordbox db.

    Returns the backup path on success. Raises a clean ``HTTPException``
    (503) instead of a silent no-op whenever the environment cannot
    actually perform this write -- e.g. a cloud sandbox with no rekordbox
    install -- so the apply is deferred to a local verify pass rather than
    pretending to have succeeded.
    """
    require_writeback_enabled("module.relocate.write_folder_path")
    if not paths.REKORDBOX_LIVE_DB.exists():
        raise HTTPException(status_code=503, detail={
            "code": "REKORDBOX_DB_UNAVAILABLE",
            "message": (
                f"no live rekordbox database at {paths.REKORDBOX_LIVE_DB}; "
                "apply this relocate on a machine with rekordbox installed"
            ),
        })
    try:
        from pyrekordbox import Rekordbox6Database
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
        candidate_descriptor: int | None = None
        candidate_guard: Callable[[], None] | None = None
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
            checked, candidate_descriptor = _open_candidate_file(
                candidate.path, expected_identity,
            )

            def _guard_candidate_path() -> None:
                assert candidate_descriptor is not None
                _assert_candidate_path_identity(
                    checked, candidate_descriptor, expected_identity,
                )

            candidate_guard = _guard_candidate_path
            content.FolderPath = checked.path
            candidate_guard()
            write_started = True
            db.commit()
            candidate_guard()
        except Exception as exc:
            write_error = exc
        try:
            db.close()
        except Exception as exc:
            if write_error is None:
                write_error = exc

        try:
            if write_error is not None:
                if backup is None or not write_started:
                    raise write_error
                _restore_live_database(backup)
                if isinstance(write_error, HTTPException):
                    raise write_error
                raise HTTPException(status_code=500, detail={
                    "code": "RELOCATE_WRITE_ROLLED_BACK",
                    "message": f"live write failed and backup was restored: {backup}",
                }) from write_error

            assert backup is not None
            try:
                verify_db = Rekordbox6Database(path=str(paths.REKORDBOX_LIVE_DB))
                try:
                    readback = verify_db.get_content(ID=vendor_id)
                    if readback is None or readback.FolderPath != candidate.path:
                        raise RuntimeError("rekordbox FolderPath readback mismatch")
                    assert candidate_guard is not None
                    candidate_guard()
                finally:
                    verify_db.close()
            except Exception as exc:
                _restore_live_database(backup)
                if isinstance(exc, HTTPException):
                    raise exc
                raise HTTPException(status_code=500, detail={
                    "code": "RELOCATE_WRITE_ROLLED_BACK",
                    "message": f"FolderPath verification failed and backup was restored: {backup}",
                }) from exc
            return str(backup)
        finally:
            if candidate_descriptor is not None:
                os.close(candidate_descriptor)


@router.post("/{stable_id}/apply", response_model=RelocateApplyOut)
def apply_relocate(
    stable_id: str,
    body: RelocateApplyIn,
    if_match: str | None = Header(None, alias="If-Match"),
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
):
    require_writeback_enabled("http.relocate.apply")
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
    if vendor_id != body.expected_vendor_id:
        raise HTTPException(status_code=409, detail={
            "code": "RELOCATE_VENDOR_MAPPING_CHANGED",
            "message": "vendor mapping changed; refresh candidates before applying",
        })
    candidate, descriptor = _open_candidate_file(
        body.new_path, body.expected_candidate_identity,
    )
    os.close(descriptor)
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
    if candidate.path not in {str(found.path.resolve()) for found in current_candidates}:
        raise HTTPException(status_code=409, detail={
            "code": "RELOCATE_CANDIDATE_STALE",
            "message": "candidate no longer matches this track; refresh candidates before applying",
        })

    if vendor_id is not None:
        backup_path = _write_rekordbox_folder_path(
            vendor_id,
            original_path,
            candidate,
            body.expected_candidate_identity,
        )
        publish("library.changed", {"kind": "reconcile", "ids": [stable_id]})
        return RelocateApplyOut(
            stable_id=stable_id, new_path=candidate.path, target="rekordbox",
            vendor_id=vendor_id, backup_path=backup_path,
        )

    updated = _mutate_with_candidate_guard(
        candidate,
        body.expected_candidate_identity,
        lambda checked, guard: backend.update_track(
            stable_id,
            {"file_path": checked.path},
            expected_etag=if_match,
            source="webui",
            mutation_guard=guard,
        ),
    )
    if (
        updated.file_path != candidate.path
        or backend.get_track(stable_id).file_path != candidate.path
    ):
        raise HTTPException(status_code=500, detail={
            "code": "RELOCATE_STATE_READBACK_FAILED",
            "message": "state-layer file_path did not read back after update",
        })
    publish("library.changed", {"kind": "reconcile", "ids": [stable_id]})
    return RelocateApplyOut(
        stable_id=stable_id, new_path=candidate.path, target="state",
        vendor_id=None, backup_path=None,
    )


__all__ = [
    "RelocateApplyIn",
    "RelocateApplyOut",
    "RelocateCandidateList",
    "RelocateCandidateOut",
    "router",
]
