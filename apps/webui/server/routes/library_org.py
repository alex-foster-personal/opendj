"""Library organization API (Library Organization PRD s7, foundation slice).

Sections, tag types, tag metadata, works and undo, plus the playlist hierarchy
fields. Every write returns the row and ``undo_id``, which is null only when
nothing changed. Errors are typed: ``NOT_FOUND`` 404, ``CONFLICT`` 409,
``INVARIANT`` and ``VALIDATION`` 422. Agents drive these same routes with
``opendj api METHOD PATH``.

``X-Undo-Session`` names the undo stack and is required on every write, so a
caller can never land on a stack it did not choose.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import library_org as org
from apps.shared.state.writer import StateWriter

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state

router = APIRouter(tags=["library-organization"])

_STATUS: dict[str, int] = {
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "CONFLICT": status.HTTP_409_CONFLICT,
    "INVARIANT": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "VALIDATION": status.HTTP_422_UNPROCESSABLE_ENTITY,
}


class WriteOut(BaseModel):
    row: dict[str, Any]
    undo_id: str | None


class DeleteOut(BaseModel):
    undo_id: str | None


class RowsOut(BaseModel):
    rows: list[dict[str, Any]]


class UndoOut(BaseModel):
    undo_id: str
    op: str


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaylistOrganizationIn(_Strict):
    parent_id: str | None = None
    kind: Literal["folder", "playlist", "set"] | None = None
    stage: Literal["inbox", "sorting", "ready", "archive"] | None = None
    set_status: Literal["draft", "ready", "performed", "background"] | None = None
    sort_index: int | None = None


class SectionCreateIn(_Strict):
    owner_kind: Literal["playlist", "set"]
    owner_id: str = Field(min_length=1)
    before_position: int = Field(ge=0)
    title: str | None = None
    color: str | None = None


class SectionUpdateIn(_Strict):
    title: str | None = Field(None, min_length=1)
    color: str | None = None
    before_position: int | None = Field(None, ge=0)


class TagTypeCreateIn(_Strict):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    singular: str = Field(min_length=1)
    show_as_column: bool = False
    show_in_tags: bool = True
    column_width: int | None = None
    sort_index: int = 0


class TagTypeUpdateIn(_Strict):
    label: str | None = Field(None, min_length=1)
    singular: str | None = Field(None, min_length=1)
    show_as_column: bool | None = None
    show_in_tags: bool | None = None
    column_width: int | None = None
    sort_index: int | None = None


class TagMetaIn(_Strict):
    type: str = "general"
    color: str | None = None
    source_category: str | None = None


class TagSynonymIn(_Strict):
    term: str = Field(min_length=1)
    tag: str = Field(min_length=1)


class WorkCreateIn(_Strict):
    kind: Literal["classical", "song"]
    title: str = Field(min_length=1)
    composer: str | None = None
    catalogue: str | None = None
    movement: str | None = None
    aliases: list[str] = Field(default_factory=list)
    musicbrainz_work_id: str | None = None
    iswc: str | None = None


class WorkUpdateIn(_Strict):
    kind: Literal["classical", "song"] | None = None
    title: str | None = Field(None, min_length=1)
    composer: str | None = None
    catalogue: str | None = None
    movement: str | None = None
    aliases: list[str] | None = None
    musicbrainz_work_id: str | None = None
    iswc: str | None = None


class WorkMemberIn(_Strict):
    member_kind: Literal["track", "edit"]
    member_id: str = Field(min_length=1)
    role: Literal["original", "recording", "remix", "cover", "edit", "live", "mashup"]
    is_primary: bool = False
    sort_index: int = 0


class WorkDismissalIn(_Strict):
    a_member: str = Field(min_length=1)
    b_member: str = Field(min_length=1)
    work_id: str | None = None


class WorkPrefIn(_Strict):
    top_member_id: str = Field(min_length=1)


@dataclass
class _Session:
    """One request's writer and undo stack."""

    writer: StateWriter
    undo_session: str

    def run(self, write: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return write(self.writer, self.undo_session, *args, **kwargs)
        except org.OrgError as exc:
            raise _http(exc) from exc


def _http(exc: org.OrgError) -> HTTPException:
    return HTTPException(
        status_code=_STATUS[exc.code], detail={"error": exc.code, "message": exc.message}
    )


def _db_path(request: Request) -> Path:
    return Path(request.app.state.state_db_path)


# FastAPI runs a sync dependency and its sync endpoint on different threadpool
# threads, so each connection below crosses a thread once per request.
def _session(
    request: Request,
    _backend: Annotated[StateBackend, Depends(get_write_state)],
    undo_session: Annotated[str, Header(alias="X-Undo-Session", min_length=1)],
) -> Iterator[_Session]:
    conn = state_db.open_rw(_db_path(request), check_same_thread=False, apply_schema=False)
    writer = StateWriter(conn, actor="library-org")
    try:
        yield _Session(writer, undo_session)
    finally:
        writer.close()
        conn.close()


def _reader(
    request: Request, _backend: Annotated[StateBackend, Depends(get_read_state)]
) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(_db_path(request), check_same_thread=False, apply_schema=False)
    try:
        yield conn
    finally:
        conn.close()


Session = Annotated[_Session, Depends(_session)]
Reader = Annotated[sqlite3.Connection, Depends(_reader)]


def _set(body: BaseModel) -> dict[str, Any]:
    """Only the fields the caller sent, so an explicit null can clear a value."""
    return body.model_dump(exclude_unset=True)


def _written(result: org.WriteResult, kind: str) -> WriteOut:
    publish("library.changed", {"kind": kind, "ids": []})
    return WriteOut(row=result.row, undo_id=result.undo_id)


def _deleted(undo_id: str | None, kind: str) -> DeleteOut:
    publish("library.changed", {"kind": kind, "ids": []})
    return DeleteOut(undo_id=undo_id)


@router.get("/playlist-tree/organization", response_model=RowsOut)
def list_organization(reader: Reader) -> RowsOut:
    rows = reader.execute(
        "SELECT playlist_id, name, parent_id, kind, stage, set_status, sort_index "
        "FROM playlists WHERE deleted_at IS NULL"
    ).fetchall()
    names = ("playlist_id", "name", "parent_id", "kind", "stage", "set_status", "sort_index")
    return RowsOut(rows=[dict(zip(names, tuple(r), strict=True)) for r in rows])


@router.put("/playlists/{playlist_id}/organization", response_model=WriteOut)
def organize_playlist(playlist_id: str, body: PlaylistOrganizationIn, session: Session) -> WriteOut:
    return _written(session.run(org.organize_playlist, playlist_id, _set(body)), "playlists")


@router.get("/sections", response_model=RowsOut)
def list_sections(
    reader: Reader,
    owner_kind: Annotated[Literal["playlist", "set"], Query()],
    owner_id: Annotated[str, Query(min_length=1)],
) -> RowsOut:
    return RowsOut(rows=org.list_rows(
        reader, "playlist_section", {"owner_kind": owner_kind, "owner_id": owner_id}
    ))


@router.post("/sections", response_model=WriteOut, status_code=status.HTTP_201_CREATED)
def add_section(body: SectionCreateIn, session: Session) -> WriteOut:
    return _written(session.run(org.add_section, **body.model_dump()), "sections")


@router.patch("/sections/{section_id}", response_model=WriteOut)
def update_section(section_id: str, body: SectionUpdateIn, session: Session) -> WriteOut:
    return _written(session.run(org.update_section, section_id, _set(body)), "sections")


@router.delete("/sections/{section_id}", response_model=DeleteOut)
def delete_section(section_id: str, session: Session) -> DeleteOut:
    return _deleted(session.run(org.delete_section, section_id), "sections")


@router.get("/tag-types", response_model=RowsOut)
def list_tag_types(reader: Reader) -> RowsOut:
    return RowsOut(rows=org.list_rows(reader, "tag_type"))


@router.post("/tag-types", response_model=WriteOut, status_code=status.HTTP_201_CREATED)
def create_tag_type(body: TagTypeCreateIn, session: Session) -> WriteOut:
    fields = body.model_dump(exclude={"id"})
    fields["show_as_column"] = int(fields["show_as_column"])
    fields["show_in_tags"] = int(fields["show_in_tags"])
    return _written(session.run(org.create_tag_type, body.id, fields), "tags")


@router.patch("/tag-types/{type_id}", response_model=WriteOut)
def update_tag_type(type_id: str, body: TagTypeUpdateIn, session: Session) -> WriteOut:
    changes = {k: int(v) if isinstance(v, bool) else v for k, v in _set(body).items()}
    return _written(session.run(org.update_tag_type, type_id, changes), "tags")


@router.get("/tag-meta", response_model=RowsOut)
def list_tag_meta(reader: Reader) -> RowsOut:
    return RowsOut(rows=org.list_rows(reader, "tag_meta"))


@router.put("/tag-meta/{name}", response_model=WriteOut)
def put_tag_meta(name: str, body: TagMetaIn, session: Session) -> WriteOut:
    return _written(session.run(
        org.put_tag_meta, name, tag_type=body.type, color=body.color,
        source_category=body.source_category,
    ), "tags")


@router.get("/tag-synonyms", response_model=RowsOut)
def list_tag_synonyms(reader: Reader) -> RowsOut:
    return RowsOut(rows=org.list_rows(reader, "tag_synonym"))


@router.post("/tag-synonyms", response_model=WriteOut)
def put_tag_synonym(body: TagSynonymIn, session: Session) -> WriteOut:
    return _written(session.run(org.put_tag_synonym, body.term, body.tag), "tags")


@router.get("/works", response_model=RowsOut)
def list_works(reader: Reader) -> RowsOut:
    return RowsOut(rows=org.list_rows(reader, "work"))


@router.post("/works", response_model=WriteOut, status_code=status.HTTP_201_CREATED)
def create_work(body: WorkCreateIn, session: Session) -> WriteOut:
    return _written(session.run(org.create_work, body.model_dump()), "works")


@router.post("/works/dismissals", response_model=WriteOut)
def dismiss_pair(body: WorkDismissalIn, session: Session) -> WriteOut:
    return _written(session.run(org.dismiss_pair, **body.model_dump()), "works")


@router.patch("/works/{work_id}", response_model=WriteOut)
def update_work(work_id: str, body: WorkUpdateIn, session: Session) -> WriteOut:
    return _written(session.run(org.update_work, work_id, _set(body)), "works")


@router.delete("/works/{work_id}", response_model=DeleteOut)
def delete_work(work_id: str, session: Session) -> DeleteOut:
    return _deleted(session.run(org.delete_work, work_id), "works")


@router.get("/works/{work_id}/members", response_model=RowsOut)
def list_members(work_id: str, reader: Reader) -> RowsOut:
    return RowsOut(rows=org.list_rows(reader, "work_member", {"work_id": work_id}))


@router.post("/works/{work_id}/members", response_model=WriteOut)
def link_member(work_id: str, body: WorkMemberIn, session: Session) -> WriteOut:
    return _written(session.run(org.link_member, work_id, **body.model_dump()), "works")


@router.delete("/works/{work_id}/members/{member_kind}/{member_id}", response_model=DeleteOut)
def unlink_member(
    work_id: str, member_kind: str, member_id: str, session: Session
) -> DeleteOut:
    return _deleted(session.run(org.unlink_member, work_id, member_kind, member_id), "works")


@router.put("/works/{work_id}/pref", response_model=WriteOut)
def set_work_pref(work_id: str, body: WorkPrefIn, session: Session) -> WriteOut:
    return _written(session.run(org.set_work_pref, work_id, body.top_member_id), "works")


@router.post("/undo", response_model=UndoOut)
def undo(session: Session) -> UndoOut:
    try:
        result = org.undo_last(session.writer, session.undo_session)
    except org.OrgError as exc:
        raise _http(exc) from exc
    publish("library.changed", {"kind": "undo", "ids": [result.undo_id]})
    return UndoOut(undo_id=result.undo_id, op=result.op)
