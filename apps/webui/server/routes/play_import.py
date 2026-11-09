"""Import another DJ app's play history into Open DJ's play log (DJIMPORT-10069).

``POST /plays/import/traktor-history`` reads every NML in ``<source>/History``
and appends its PLAYEDPUBLIC entries as ``track_played`` events, exactly as
``python -m apps.adapters.library_import traktor-history`` does, so an agent
can drive the import without a UI. A re-run adds nothing.

Mounted from ``app_wiring.py`` under the API prefix.
"""
from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.adapters import library_import
from apps.adapters.rekordbox import config as rb_config
from apps.adapters.traktor.history import HISTORY_DIRNAME
from apps.shared.events import publish
from apps.shared.state import db as state_db

from ..backend import StateBackend
from ..deps import get_write_state

router = APIRouter(prefix="/plays", tags=["play-import"])
MOUNT_UNDER_API_PREFIX = True


class TraktorHistoryImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, description="Absolute path of the Traktor folder holding History/.")
    zone: str | None = Field(default=None, description="IANA zone of the history times; null = this machine's.")
    dry_run: bool = Field(description="True rolls the import back after counting.")


class RejectedFileOut(BaseModel):
    file: str
    reason: str


class TraktorHistoryImportOut(BaseModel):
    source: str
    zone: str
    dry_run: bool
    files_read: list[str]
    files_rejected: list[RejectedFileOut]
    entries_seen: int
    entries_not_public: int
    entries_unreadable: int
    plays_matched: int
    plays_unmatched: int
    plays_added: int
    plays_already_logged: int


def _state_db_path(request: Request) -> Path:
    path = getattr(request.app.state, "state_db_path", None)
    return Path(path) if path is not None else rb_config.STATE_DB


@router.post("/import/traktor-history", response_model=TraktorHistoryImportOut)
def import_traktor_history(
    body: TraktorHistoryImportIn,
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TraktorHistoryImportOut:
    source = Path(body.source)
    if not source.is_absolute():
        raise HTTPException(422, detail={"code": "SOURCE_NOT_ABSOLUTE", "message": body.source})
    if not (source / HISTORY_DIRNAME).is_dir():
        raise HTTPException(404, detail={"code": "TRAKTOR_HISTORY_NOT_FOUND", "message": str(source)})
    try:
        zone = ZoneInfo(body.zone) if body.zone else None
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(422, detail={"code": "BAD_ZONE", "message": str(body.zone)}) from exc
    conn = state_db.open_rw(_state_db_path(request))
    try:
        report = library_import.import_traktor_history(
            conn, source, zone=zone, dry_run=body.dry_run, actor="webui"
        )
    finally:
        conn.close()
    if report.plays_added and not report.dry_run:
        publish("library.changed", {"kind": "plays", "ids": report.added_stable_ids})
    data = report.to_dict()
    data.pop("added_stable_ids")
    data["files_rejected"] = [{"file": name, "reason": reason} for name, reason in report.files_rejected]
    return TraktorHistoryImportOut(**data)


__all__ = ["TraktorHistoryImportIn", "TraktorHistoryImportOut", "router"]
