"""HTTP parity for non-destructive virtual track edits (LIBEDIT-9179)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.events import publish
from apps.shared.state import library_edits

from .library_org import Reader, Session

router = APIRouter(prefix="/edits", tags=["library-edits"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EditCreateIn(_Strict):
    source_track_id: str = Field(min_length=1)
    label: str = ""
    in_ms: int
    out_ms: int
    fx: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    playlist_id: str | None = None
    after_item_id: str | None = None


class EditUpdateIn(_Strict):
    label: str | None = None
    in_ms: int | None = None
    out_ms: int | None = None
    fx: dict[str, Any] | None = None


class WriteOut(BaseModel):
    row: dict[str, Any]
    undo_id: str | None


class DeleteOut(BaseModel):
    undo_id: str | None


class RowsOut(BaseModel):
    rows: list[dict[str, Any]]


def _changed(kind: str) -> None:
    publish("library.changed", {"kind": kind, "ids": []})


@router.get("", response_model=RowsOut)
def list_track_edits(reader: Reader, source_track_id: str | None = None) -> RowsOut:
    return RowsOut(rows=library_edits.list_edits(reader, source_track_id))


@router.post("", response_model=WriteOut, status_code=status.HTTP_201_CREATED)
def create_track_edit(body: EditCreateIn, session: Session) -> WriteOut:
    values = body.model_dump()
    values["created_by"] = body.created_by or session.undo_session
    result = session.run(library_edits.create_edit, **values)
    _changed("edits")
    return WriteOut(row=result.row, undo_id=result.undo_id)


@router.patch("/{edit_id}", response_model=WriteOut)
def update_track_edit(edit_id: str, body: EditUpdateIn, session: Session) -> WriteOut:
    result = session.run(
        library_edits.update_edit,
        edit_id,
        body.model_dump(exclude_unset=True, exclude_none=True),
    )
    _changed("edits")
    return WriteOut(row=result.row, undo_id=result.undo_id)


@router.delete("/{edit_id}", response_model=DeleteOut)
def delete_track_edit(edit_id: str, session: Session) -> DeleteOut:
    undo_id = session.run(library_edits.delete_edit, edit_id)
    _changed("edits")
    return DeleteOut(undo_id=undo_id)


__all__ = ["router"]
