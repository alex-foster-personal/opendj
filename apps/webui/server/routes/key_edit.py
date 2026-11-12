"""The library key cell's stored edit over HTTP (KEY-10229).

The key cell and any agent drive the SAME routes (CLI twin: ``opendj track
key-edit``):

  GET    /api/v1/tracks/{stable_id}/key-edit    the edit in force (key null when none)
  PUT    /api/v1/tracks/{stable_id}/key-edit    {"key": "Gm"}: any notation, stored as Camelot ("6A")
  DELETE /api/v1/tracks/{stable_id}/key-edit    forget it (history keeps the earlier value)

A key no notation parses is 422 ``KEY_EDIT_INVALID``. The edit is the
``track_fields`` row ``key_edit`` (``apps/shared/state/key_edit.py``); the
library row, ``GET /tracks/{id}`` and ``/anlz`` serve it as ``key_source:
"edit"``. Every write publishes ``library.changed`` kind ``tracks``.

-Claude
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from apps.analysis_key.canon import canonical_key_text
from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import key_edit
from apps.shared.state.sync_stamp import canonical_now
from apps.shared.state.writer import StateWriter

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..rb_vendor_pkg.own_lane_store import state_conn_ro
from .grid_edit import _require_track, state_db_path


class KeyEditError(ValueError):
    """A key no supported notation parses."""

    status = 422
    code = key_edit.INVALID_CODE


def canonical_edit_key(raw: str) -> str:
    """Any notation ``canonical_key_text`` parses, as canonical Camelot; raises KeyEditError.

    It lives in the route, not ``apps.shared.state.key_edit``, because apps.shared imports nothing else from apps.
    """
    # canonical_key_text answers the raw text when nothing parses and None for
    # a no-key sentinel, so only a Camelot answer is a key.
    text = canonical_key_text(raw.strip())
    if text is None or not key_edit.CAMELOT.fullmatch(text):
        raise KeyEditError(f"{raw!r} is not a key in any supported notation")
    return text


router = APIRouter(prefix="/tracks", tags=["key-edit"])
MOUNT_UNDER_API_PREFIX = True


class KeyEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The key in any notation the server parses (Camelot, Open Key, musical).
    key: str


class KeyEditOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    #: The stored key as canonical Camelot; null when none.
    key: str | None
    #: When the edit in force was written; null when never edited.
    modified_at: str | None


def _write(request: Request, stable_id: str, key: str | None) -> KeyEditOut:
    conn = state_db.open_rw(state_db_path(request))
    try:
        with StateWriter(conn, actor="webui") as writer, writer.transaction():
            _require_track(conn, stable_id)
            writer.set_field(
                stable_id, key_edit.FIELD, key_edit.blob(key), source=key_edit.OWN_SOURCE, modified_at=canonical_now()
            )
            stored, modified_at = key_edit.read_row(conn, stable_id)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    publish("library.changed", {"kind": "tracks", "ids": [stable_id]})
    return KeyEditOut(stable_id=stable_id, key=stored, modified_at=modified_at)


@router.get("/{stable_id}/key-edit", response_model=KeyEditOut)
def get_key_edit(
    request: Request,
    stable_id: str,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> KeyEditOut:
    conn = state_conn_ro(state_db_path(request))
    if conn is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "STATE_DB_UNAVAILABLE", "message": "no state database to read"},
        )
    try:
        _require_track(conn, stable_id)
        key, modified_at = key_edit.read_row(conn, stable_id)
    finally:
        conn.close()
    return KeyEditOut(stable_id=stable_id, key=key, modified_at=modified_at)


@router.put("/{stable_id}/key-edit", response_model=KeyEditOut)
def set_key_edit(
    request: Request,
    stable_id: str,
    body: KeyEditIn,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> KeyEditOut:
    try:
        key = canonical_edit_key(body.key)
    except KeyEditError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
    return _write(request, stable_id, key)


@router.delete("/{stable_id}/key-edit", response_model=KeyEditOut)
def clear_key_edit(
    request: Request,
    stable_id: str,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> KeyEditOut:
    """Back to the analysed key. A write of ``{"key": null}``, so history keeps the old edit."""
    return _write(request, stable_id, None)
