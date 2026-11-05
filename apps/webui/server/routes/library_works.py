"""Version stacks, work suggestions and classical metadata (PRD s7).

Sits beside :mod:`library_org` and shares its session, reader and error shapes.
Nothing here links a track without an explicit write. Agents drive these routes
with ``opendj work ...`` or ``opendj api METHOD PATH``.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from apps.shared import _tagreader, classical_tags
from apps.shared.state import library_org as org
from apps.shared.state import library_org_works as org_works
from apps.shared.state import library_works as works

from .library_org import Reader, RowsOut, Session, WriteOut, _set, _Strict, _written

router = APIRouter(tags=["library-organization"])

Role = Literal["original", "recording", "remix", "cover", "edit", "live", "mashup"]


class WorkLinkIn(_Strict):
    member_ids: list[str] = Field(min_length=1)
    work_id: str | None = None
    title: str | None = None
    kind: Literal["classical", "song"] = "song"
    role: Role = "remix"


class WorkMergeIn(_Strict):
    into: str = Field(min_length=1)


class WorkMoveIn(_Strict):
    member_ids: list[str] = Field(min_length=1)


class WorkMemberUpdateIn(_Strict):
    role: Role | None = None
    is_primary: bool | None = None


class SuggestionFlagsIn(_Strict):
    member_ids: list[str]


class SuggestionFlagsOut(BaseModel):
    member_ids: list[str]


class WorkOverlayOut(BaseModel):
    works: dict[str, dict[str, Any]]
    members: dict[str, list[dict[str, Any]]]
    classical: dict[str, dict[str, Any]]


class WorkStackOut(BaseModel):
    work: dict[str, Any] | None
    members: list[dict[str, Any]]
    suggestions: list[dict[str, Any]]


class ClassicalIn(_Strict):
    composer: str | None = None
    catalogue: str | None = None
    movement: str | None = None
    soloist: str | None = None
    ensemble: str | None = None
    conductor: str | None = None
    label: str | None = None
    recording_year: int | None = None


class ClassicalOut(BaseModel):
    values: dict[str, Any]


class FileTagsOut(BaseModel):
    proposed: dict[str, Any]


@router.get("/works/overlay", response_model=WorkOverlayOut)
def works_overlay(reader: Reader) -> WorkOverlayOut:
    return WorkOverlayOut(**works.overlay(reader))


@router.get("/works/stack", response_model=WorkStackOut)
def work_stack(
    reader: Reader,
    member: Annotated[str, Query(min_length=1)],
    playlist_id: Annotated[str | None, Query()] = None,
) -> WorkStackOut:
    return WorkStackOut(**works.stack(reader, member, playlist_id))


@router.get("/works/suggest", response_model=RowsOut)
def suggest_versions(reader: Reader, member: Annotated[str, Query(min_length=1)]) -> RowsOut:
    return RowsOut(rows=works.suggest(reader, member))


@router.post("/works/suggestion-flags", response_model=SuggestionFlagsOut)
def suggestion_flags(body: SuggestionFlagsIn, reader: Reader) -> SuggestionFlagsOut:
    return SuggestionFlagsOut(member_ids=works.suggestion_flags(reader, body.member_ids))


@router.get("/works/link-candidates", response_model=RowsOut)
def link_candidates(
    reader: Reader, member: Annotated[str, Query(min_length=1)], q: str = ""
) -> RowsOut:
    return RowsOut(rows=works.link_candidates(reader, member, q))


@router.post("/works:link", response_model=WriteOut)
def link_tracks(body: WorkLinkIn, session: Session) -> WriteOut:
    fields = body.model_dump(exclude={"member_ids"})
    return _written(session.run(org_works.link_tracks, body.member_ids, **fields), "works")


@router.post("/works/{work_id}:merge", response_model=WriteOut)
def merge_works(work_id: str, body: WorkMergeIn, session: Session) -> WriteOut:
    return _written(session.run(org_works.merge_works, work_id, body.into), "works")


@router.post("/works/{work_id}/members:move", response_model=WriteOut)
def move_members(work_id: str, body: WorkMoveIn, session: Session) -> WriteOut:
    return _written(session.run(org_works.move_members, work_id, body.member_ids), "works")


@router.patch("/works/{work_id}/members/{member_kind}/{member_id}", response_model=WriteOut)
def update_member(
    work_id: str, member_kind: str, member_id: str, body: WorkMemberUpdateIn, session: Session
) -> WriteOut:
    return _written(
        session.run(org_works.update_member, work_id, member_kind, member_id, _set(body)), "works"
    )


@router.get("/tracks/{stable_id}/classical", response_model=ClassicalOut)
def get_classical(stable_id: str, reader: Reader) -> ClassicalOut:
    return ClassicalOut(values=org_works.classical_values(reader, stable_id))


@router.patch("/tracks/{stable_id}/classical", response_model=WriteOut)
def set_classical(stable_id: str, body: ClassicalIn, session: Session) -> WriteOut:
    return _written(session.run(org_works.set_classical, stable_id, _set(body)), "classical")


@router.get("/tracks/{stable_id}/classical/file-tags", response_model=FileTagsOut)
def read_file_tags(stable_id: str, reader: Reader) -> FileTagsOut:
    """What the file's tags say. A proposal only: nothing is stored or linked."""
    row = reader.execute(
        "SELECT file_path FROM tracks WHERE stable_id = ? AND deleted_at IS NULL", (stable_id,)
    ).fetchone()
    if row is None or not row[0]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NOT_FOUND", "message": f"track {stable_id} has no file"},
        )
    try:
        return FileTagsOut(proposed=classical_tags.read_proposal(row[0]))
    except _tagreader.TagReadError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NOT_FOUND", "message": f"cannot read tags: {exc}"},
        ) from exc
