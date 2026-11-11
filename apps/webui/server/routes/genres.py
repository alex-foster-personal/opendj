"""Genre findings, inference, similarity, config and file-tag writes over HTTP (GENRE-03..07).

Every route has a CLI twin (``python -m apps.genre_infer <cmd>``) and an MCP
tool (``apps/opendj_cli/mcp_genre_tools.py``); all three call
:mod:`apps.genre_infer.service`.

    GET    /api/v1/genres/status
    GET    /api/v1/genres?ids=a&ids=b            all providers + chosen, per track
    POST   /api/v1/genres/findings               attach an external finding
    DELETE /api/v1/genres/findings/{sid}/{provider}
    POST   /api/v1/genres/infer                  run now (<= 500 ids), cached by request digest
    POST   /api/v1/genres/enqueue                queue tracks (or every untagged present track)
    GET    /api/v1/genres/{sid}/similar
    GET    /api/v1/genres/config  PUT /api/v1/genres/config
    PUT    /api/v1/genres/keys/{name}            store a provider key in the Keychain (GENRE-09)
    POST   /api/v1/genres/tags/write  POST /api/v1/genres/tags/undo  GET /api/v1/genres/tags/journal

-Claude
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.analysis import queue_user
from apps.analysis import store as analysis_store
from apps.analysis.queue import QueueError
from apps.analysis.queue_user_lanes import MAX_ENQUEUE_IDS
from apps.genre_infer import findings, keystore, pipeline, providers, service, tag_write
from apps.shared.events import publish
from apps.shared.paths import STATE_DB
from apps.shared.state import db as state_db

from ..backend import StateBackend
from ..deps import get_library_data_dir, get_write_state

router = APIRouter(prefix="/genres", tags=["genres"])
MOUNT_UNDER_API_PREFIX = True

KEY_MISSING_CODE = "genre_key_missing"


def _db_path(request: Request) -> Path:
    return Path(getattr(request.app.state, "state_db_path", STATE_DB))


def _require_db(request: Request) -> Path:
    path = _db_path(request)
    if not path.is_file():
        raise HTTPException(503, {"code": "no_library_db", "message": f"no library database at {path}"})
    return path


@contextmanager
def _read(request: Request) -> Iterator[sqlite3.Connection]:
    """A read-only connection: a GET never creates tables or files."""
    conn = state_db.open_ro(_require_db(request))
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write(request: Request) -> Iterator[sqlite3.Connection]:
    conn = analysis_store.open_conn(_require_db(request))
    try:
        findings.ensure_schema(conn)
        conn.commit()
        yield conn
    finally:
        conn.close()


def _bad(exc: Exception) -> HTTPException:
    if isinstance(exc, service.UnknownTrack):
        return HTTPException(404, {"code": "unknown_track", "message": str(exc).strip("'\"")})
    if isinstance(exc, providers.ProviderUnavailable):
        return HTTPException(409, {"code": KEY_MISSING_CODE, "message": str(exc), "missing_key": exc.key_env})
    if isinstance(exc, tag_write.TagWriteRefused):
        return HTTPException(409, {"code": "tag_write_refused", "message": str(exc)})
    if isinstance(exc, pipeline.InferenceBusy):
        return HTTPException(409, {"code": "genre_inference_busy", "message": str(exc)})
    if isinstance(exc, KeyError):
        return HTTPException(404, {"code": "not_found", "message": str(exc).strip("'\"")})
    return HTTPException(422, {"code": "invalid", "message": str(exc)})


_HANDLED = (
    findings.GenreInputError,
    service.UnknownTrack,
    providers.ProviderUnavailable,
    tag_write.TagWriteRefused,
    pipeline.InferenceBusy,
    KeyError,
)


# ------------------------------------------------------------------ models
class FindingIn(BaseModel):
    stable_id: str
    provider: str = Field(description="Your source's id, e.g. 'beatport'. Open DJ's own ids are refused.")
    genre: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = Field(min_length=1, description="Where the genre came from: a URL or a name.")
    note: str | None = None
    model: str | None = None


class InferIn(BaseModel):
    stable_ids: list[str] = Field(min_length=1, max_length=service.MAX_IDS)
    force: bool = False


class EnqueueIn(BaseModel):
    stable_ids: list[str] | None = Field(default=None, max_length=MAX_ENQUEUE_IDS)
    all_missing: bool = Field(
        default=False, description="Queue every live track with local audio and no fused genre yet."
    )


class TagWriteIn(BaseModel):
    stable_id: str


class TagUndoIn(BaseModel):
    write_id: int


# ------------------------------------------------------------------ routes
@router.get("/status")
def genre_status(request: Request) -> dict[str, Any]:
    with _read(request) as conn:
        return service.status(conn)


@router.get("")
def get_genres(request: Request, ids: list[str] = Query(min_length=1)) -> dict[str, Any]:  # noqa: B008
    with _read(request) as conn:
        try:
            return service.genres_for(conn, ids)
        except _HANDLED as exc:
            raise _bad(exc) from exc


@router.post("/findings", status_code=201)
def attach_finding(
    request: Request,
    body: FindingIn,
    _guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> dict[str, Any]:
    with _write(request) as conn:
        try:
            out = service.attach_finding(conn, **body.model_dump())
        except _HANDLED as exc:
            raise _bad(exc) from exc
    publish("library.changed", {"kind": "tracks", "ids": [body.stable_id]})
    return out


@router.delete("/findings/{stable_id}/{provider}")
def remove_finding(
    request: Request,
    stable_id: str,
    provider: str,
    _guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> dict[str, Any]:
    with _write(request) as conn:
        try:
            removed = service.remove_finding(conn, stable_id=stable_id, provider=provider)
        except _HANDLED as exc:
            raise _bad(exc) from exc
    if not removed:
        raise HTTPException(404, {"code": "not_found", "message": f"no {provider} finding for {stable_id}"})
    publish("library.changed", {"kind": "tracks", "ids": [stable_id]})
    return {"removed": True}


@router.post("/infer")
def infer_now(
    request: Request,
    body: InferIn,
    _guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> dict[str, Any]:
    with _write(request) as conn:
        try:
            out = service.infer(conn, body.stable_ids, force=body.force)
        except _HANDLED as exc:
            raise _bad(exc) from exc
    publish("library.changed", {"kind": "tracks", "ids": body.stable_ids})
    return out


@router.post("/enqueue", status_code=201)
def enqueue(
    request: Request,
    body: EnqueueIn,
    _guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> dict[str, Any]:
    if bool(body.stable_ids) == body.all_missing:
        raise HTTPException(422, {"code": "invalid", "message": "give stable_ids or all_missing=true, not both"})
    missing = providers.missing_keys()
    if missing:
        raise _bad(providers.ProviderUnavailable(missing[0]))
    data_dir = get_library_data_dir(request)
    with _write(request) as conn:
        if body.stable_ids:
            ids = pipeline.attention_infer_ids(conn, body.stable_ids)
        else:
            ids = pipeline.missing_fused_ids(conn, pipeline.eligible_ids(conn))
        queued = 0
        touched: list[str] = []
        try:
            for start in range(0, len(ids), MAX_ENQUEUE_IDS):
                chunk = ids[start : start + MAX_ENQUEUE_IDS]
                if not chunk:
                    continue
                result = queue_user.enqueue_next(
                    conn,
                    lane="genre",
                    stable_ids=chunk,
                    placement="tail",
                    stems_root=data_dir / "state" / "stems",
                    data_dir=data_dir,
                )
                queued += len(result.items)
                touched.extend(chunk)
        except QueueError as exc:
            raise HTTPException(422, {"code": "queue_refused", "message": str(exc)}) from exc
    if touched:
        publish("library.changed", {"kind": "library_jobs", "ids": touched})
    return {"lane": "genre", "queued": queued, "requested": len(body.stable_ids or []), "eligible": len(ids)}


@router.get("/{stable_id}/similar")
def similar(request: Request, stable_id: str, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    with _read(request) as conn:
        try:
            return service.similar(conn, stable_id, limit)
        except _HANDLED as exc:
            raise _bad(exc) from exc


@router.get("/config")
def get_config(request: Request) -> dict[str, Any]:
    with _read(request) as conn:
        return findings.read_config(conn).as_dict()


@router.put("/config")
def put_config(
    request: Request,
    patch: dict[str, Any],
    _guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> dict[str, Any]:
    with _write(request) as conn:
        try:
            cfg = findings.update_config(conn, patch)
        except _HANDLED as exc:
            raise _bad(exc) from exc
        conn.commit()
    publish("library.changed", {"kind": "tracks", "ids": []})
    return cfg.as_dict()


class KeyBody(BaseModel):
    value: str = Field(min_length=1)


@router.put("/keys/{name}")
def put_key(
    name: str,
    body: KeyBody,
    _guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> dict[str, Any]:
    try:
        return service.set_key(name, body.value)
    except findings.GenreInputError as exc:
        raise HTTPException(400, {"code": "unknown_genre_key", "message": str(exc)}) from exc
    except keystore.KeystoreError as exc:
        raise HTTPException(503, {"code": "keystore_unavailable", "message": str(exc)}) from exc


@router.post("/tags/write")
def write_tag(
    request: Request,
    body: TagWriteIn,
    _guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> dict[str, Any]:
    with _write(request) as conn:
        try:
            return service.write_chosen_tag(conn, data_dir=get_library_data_dir(request), stable_id=body.stable_id)
        except _HANDLED as exc:
            raise _bad(exc) from exc


@router.post("/tags/undo")
def undo_tag(
    request: Request,
    body: TagUndoIn,
    _guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> dict[str, Any]:
    with _write(request) as conn:
        try:
            return tag_write.undo(conn, body.write_id).__dict__
        except _HANDLED as exc:
            raise _bad(exc) from exc


@router.get("/tags/journal")
def tag_journal(
    request: Request, stable_id: str | None = None, limit: int = Query(100, ge=1, le=1000)
) -> dict[str, Any]:
    with _read(request) as conn:
        return {"writes": tag_write.journal(conn, stable_id, limit)}
