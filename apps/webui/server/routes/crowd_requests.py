"""Crowd request engine routes (CROWDREQ-10749). Mounted under /api/v1/requests.

  GET    /requests/session
  PUT    /requests/session  {open, catalog_playlist_id, free_text_allowed, cooldown_s}
  GET    /requests?status=new|loaded|dismissed|all
  POST   /requests          {stable_id | text, requester_name, requester_key}
  POST   /requests/{id}/vote
  POST   /requests/{id}:dismiss
  POST   /requests/{id}:loaded
  DELETE /requests

The public listener (#10445) posts into POST /requests and votes through
POST /{id}/vote. The phone remote (#10448) forwards GET ?status=new plus
:dismiss, :loaded and PUT /session.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.webui.server import crowd_requests as store
from apps.webui.server.backend import StateBackend, Track
from apps.webui.server.deps import get_backend, get_library_state_db
from apps.webui.server.errors import ErrorBody

router = APIRouter(prefix="/requests", tags=["requests"])
MOUNT_UNDER_API_PREFIX = True

StatusFilter = Literal["new", "loaded", "dismissed", "all"]
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}
_INVALID: dict[int | str, dict[str, Any]] = {422: {"model": ErrorBody}}


class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: bool | None = None
    catalog_playlist_id: str | None = None
    free_text_allowed: bool | None = None
    cooldown_s: int | None = Field(default=None, gt=0)


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stable_id: str | None = None
    text: str | None = None
    requester_name: str = Field(min_length=1, max_length=store.NAME_MAX)
    requester_key: str = Field(min_length=1)


@contextmanager
def _connect(path: Path, *, write: bool) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(path)
    try:
        if write:
            with state_db.write_unit(conn, "crowd_requests"):
                store.ensure_schema(conn)
                yield conn
        else:
            store.ensure_schema(conn)
            yield conn
    finally:
        conn.close()


def _error(code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=code, content=ErrorBody(error=error, message=message).model_dump())


def _not_found(request_id: str) -> JSONResponse:
    return _error(status.HTTP_404_NOT_FOUND, "request_not_found", f"no request {request_id}")


@router.get("/session")
def get_session(path: Path = Depends(get_library_state_db)) -> dict[str, Any]:  # noqa: B008
    with _connect(path, write=False) as conn:
        return store.get_session(conn).to_json()


@router.put("/session", response_model=None, responses=_INVALID)
def put_session(
    body: SessionIn,
    path: Path = Depends(get_library_state_db),  # noqa: B008
) -> dict[str, Any] | JSONResponse:
    changes: dict[str, Any] = body.model_dump(exclude_unset=True)
    try:
        with _connect(path, write=True) as conn:
            return store.set_session(conn, **changes).to_json()
    except store.SessionError as error:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, error.code, error.message)


def _title_fields(track: Track | None) -> tuple[str, str]:
    if track is None:
        return "", ""
    return (track.title or "", track.artist or "")


def _with_titles(rows: list[dict[str, Any]], backend: StateBackend) -> list[dict[str, Any]]:
    ids = [str(row.get("stable_id") or "") for row in rows if row.get("kind") == "track" and row.get("stable_id")]
    tracks = backend.get_tracks_bulk(ids) if ids else {}
    for row in rows:
        if row.get("kind") == "track":
            title, artist = _title_fields(tracks.get(str(row.get("stable_id") or "")))
        else:
            title, artist = "", ""
        row["title"] = title
        row["artist"] = artist
    return rows


@router.get("")
def list_requests(
    status_filter: Annotated[StatusFilter, Query(alias="status")] = "new",
    path: Path = Depends(get_library_state_db),  # noqa: B008
    backend: StateBackend = Depends(get_backend),  # noqa: B008
) -> dict[str, Any]:
    with _connect(path, write=False) as conn:
        rows = [row.to_json() for row in store.list_requests(conn, status_filter)]
    return {"requests": _with_titles(rows, backend)}


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None, responses=_INVALID)
def post_request(
    body: RequestIn,
    path: Path = Depends(get_library_state_db),  # noqa: B008
) -> dict[str, Any] | JSONResponse:
    try:
        with _connect(path, write=True) as conn:
            row = store.add_request(
                conn,
                stable_id=body.stable_id,
                text=body.text,
                requester_name=body.requester_name,
                requester_key=body.requester_key,
            )
    except store.RequestError as error:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, error.code, error.message)
    publish(store.ADDED_TOPIC, row.to_json())
    return row.to_json()


@router.post("/{request_id}/vote", response_model=None, responses=_NOT_FOUND)
def vote_request(
    request_id: str,
    path: Path = Depends(get_library_state_db),  # noqa: B008
) -> dict[str, Any] | JSONResponse:
    with _connect(path, write=True) as conn:
        row = store.vote(conn, request_id)
    if row is None:
        return _not_found(request_id)
    return row.to_json()


@router.post("/{request_id}:dismiss", response_model=None, responses=_NOT_FOUND)
def dismiss_request(
    request_id: str,
    path: Path = Depends(get_library_state_db),  # noqa: B008
) -> dict[str, Any] | JSONResponse:
    with _connect(path, write=True) as conn:
        row = store.set_status(conn, request_id, "dismissed")
    if row is None:
        return _not_found(request_id)
    return row.to_json()


@router.post("/{request_id}:loaded", response_model=None, responses=_NOT_FOUND)
def loaded_request(
    request_id: str,
    path: Path = Depends(get_library_state_db),  # noqa: B008
) -> dict[str, Any] | JSONResponse:
    with _connect(path, write=True) as conn:
        row = store.set_status(conn, request_id, "loaded")
    if row is None:
        return _not_found(request_id)
    return row.to_json()


@router.delete("")
def delete_requests(path: Path = Depends(get_library_state_db)) -> dict[str, int]:  # noqa: B008
    with _connect(path, write=True) as conn:
        return {"removed": store.clear_requests(conn)}


__all__ = ["router"]
