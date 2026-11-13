"""Import an Apple Music or iTunes Library XML as playlists (issue #9752).

``POST /import/itunes-xml {source, dry_run}`` reads the ``Library.xml`` at ``source`` and writes
one Open DJ playlist per iTunes playlist under an ``iTunes`` folder, tracks matched to the
collection by file location, exactly as :mod:`apps.adapters.itunes_xml` does. Unmatched
locations come back in the report. A re-run updates the same playlists in place. Agents reach
it with ``opendj api POST /api/v1/import/itunes-xml --json '{"source": ..., "dry_run": false}'``.

Mounted from ``app_wiring.py`` under the API prefix.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from apps.adapters import itunes_xml
from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

from ..backend import StateBackend
from ..deps import get_library_state_db, get_write_state

router = APIRouter(prefix="/import", tags=["itunes-import"])
MOUNT_UNDER_API_PREFIX = True


class ItunesImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, description="Absolute path of the iTunes or Apple Music Library XML file.")
    dry_run: bool = Field(description="True rolls the import back after counting.")


class ItunesImportOut(BaseModel):
    source: str
    dry_run: bool
    playlists_written: int
    folders_written: int
    entries_matched: int
    entries_unmatched: int
    unmatched_locations: list[str] = Field(description="Locations with no library track at that path, first-seen order")
    skipped: dict[str, int] = Field(description="Playlists skipped, by reason: master, built_in, smart")
    playlist_ids: list[str]


@router.post("/itunes-xml", response_model=ItunesImportOut)
def import_itunes_xml(
    body: ItunesImportIn,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
    state_db_path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> ItunesImportOut:
    """Import the Library XML's playlists; 404 when the file is missing, 422 when it is not one."""
    source = Path(body.source)
    if not source.is_absolute():
        raise HTTPException(422, detail={"code": "SOURCE_NOT_ABSOLUTE", "message": body.source})
    if not source.is_file():
        raise HTTPException(404, detail={"code": "ITUNES_XML_NOT_FOUND", "message": str(source)})
    try:
        library = itunes_xml.read_itunes_xml(source)
    except itunes_xml.ItunesXmlError as error:
        raise HTTPException(422, detail={"code": "INVALID_ITUNES_XML", "message": str(error)}) from error
    conn = state_db.open_rw(state_db_path)
    try:
        with StateWriter(conn, actor="webui") as writer:
            report = itunes_xml.import_itunes_library(writer, library, source=str(source), dry_run=body.dry_run)
    finally:
        conn.close()
    if not report.dry_run and report.playlist_ids:
        publish("library.changed", {"kind": "playlists", "ids": report.playlist_ids})
    return ItunesImportOut(**vars(report))


__all__ = ["ItunesImportIn", "ItunesImportOut", "router"]
