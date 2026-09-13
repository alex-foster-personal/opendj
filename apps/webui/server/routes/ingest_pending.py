"""Pending ingest batches awaiting manual Rekordbox import.

Split from :mod:`apps.webui.server.routes.ingest` so ingest.py stays under
the line-count ceiling. Lists staged batches under INGEST_INBOX that have
audio files but no ``.rb-imported`` marker; confirm writes the marker only.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from apps.shared.paths import AUDIO_EXTENSIONS
from apps.webui.server.routes import ingest as ingest_cfg

router = APIRouter(prefix="/ingest", tags=["ingest"])

RB_IMPORTED_MARKER = ".rb-imported"


class PendingBatch(BaseModel):
    name: str
    dest_dir: str
    file_count: int
    awaiting_rb: bool = True


class PendingOut(BaseModel):
    batches: list[PendingBatch]


def _batch_audio_count(batch_dir: Path) -> int:
    count = 0
    for path in batch_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".part"):
            continue
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            count += 1
    return count


def _list_pending() -> list[PendingBatch]:
    inbox = ingest_cfg.INGEST_INBOX
    if not inbox.is_dir():
        return []
    batches: list[PendingBatch] = []
    for child in sorted(inbox.iterdir()):
        if not child.is_dir():
            continue
        if not ingest_cfg.BATCH_RE.match(child.name):
            continue
        if (child / RB_IMPORTED_MARKER).exists():
            continue
        file_count = _batch_audio_count(child)
        if file_count == 0:
            continue
        batches.append(
            PendingBatch(
                name=child.name,
                dest_dir=str(child),
                file_count=file_count,
            )
        )
    return batches


@router.get("/pending", response_model=PendingOut)
def list_pending() -> PendingOut:
    return PendingOut(batches=_list_pending())


@router.post("/pending/{batch}/confirm", status_code=204)
def confirm_pending(batch: str) -> None:
    if not ingest_cfg.BATCH_RE.match(batch):
        raise HTTPException(422, f"invalid batch name {batch!r}")
    dest_dir = ingest_cfg.INGEST_INBOX / batch
    if not dest_dir.is_dir():
        raise HTTPException(404, f"batch {batch!r} not found")
    (dest_dir / RB_IMPORTED_MARKER).write_text("")

