"""Materialize a staged ingest batch into state.db via the folder adapter.

POST /ingest/batch/{batch}/materialize runs ``folder_ingest.ingest_folder`` on
the bytes already written under INGEST_INBOX/<batch>/ so playlist-folder drop
and similar flows can resolve stable_ids for newly staged files without a
Rekordbox import step.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from apps.shared.paths import AUDIO_EXTENSIONS
from apps.shared.state import db as state_db
from apps.shared.state.ingest import folder as folder_ingest
from apps.shared.state.writer import StateWriter
from apps.webui.server.routes import ingest as ingest_cfg

router = APIRouter(prefix="/ingest", tags=["ingest"])


class MaterializedTrack(BaseModel):
    relative_path: str
    stable_id: str
    inserted: bool


class MaterializeOut(BaseModel):
    batch: str
    tracks: list[MaterializedTrack]


def _staged_audio_files(dest_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in dest_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".part"):
            continue
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            files.append(path)
    return sorted(files)


def _stable_id_for_path(conn, file_path: Path) -> str | None:
    row = conn.execute(
        "SELECT stable_id FROM tracks WHERE file_path = ? AND deleted_at IS NULL",
        (str(file_path.resolve()),),
    ).fetchone()
    return None if row is None else str(row[0])


def _resolve_staged(
    conn, dest_dir: Path, staged: list[Path], before_ids: set[str]
) -> list[MaterializedTrack]:
    """Resolve every staged path to its track row, or raise 422 naming the misses.

    Runs inside the still-open write transaction so the caller commits only
    when all of them resolved. A staged file the folder adapter rejected (an
    allowlisted extension over an unplayable payload) would otherwise leave its
    valid peers durable while the caller is told the whole call failed.
    """
    tracks: list[MaterializedTrack] = []
    unresolved: list[str] = []
    for path in staged:
        rel = str(path.relative_to(dest_dir))
        stable_id = _stable_id_for_path(conn, path)
        if stable_id is None:
            unresolved.append(rel)
            continue
        tracks.append(
            MaterializedTrack(
                relative_path=rel,
                stable_id=stable_id,
                inserted=stable_id not in before_ids,
            )
        )
    if unresolved:
        raise HTTPException(
            422,
            {
                "code": "MATERIALIZE_UNRESOLVED",
                "message": (
                    "materialize wrote no track row for staged file(s) "
                    f"{unresolved!r}; nothing was committed"
                ),
                "unresolved": unresolved,
            },
        )
    return tracks


@router.post("/batch/{batch}/materialize", response_model=MaterializeOut)
def materialize_batch(batch: str, request: Request) -> MaterializeOut:
    """[if] batch is staged under the ingest inbox [then] folder-ingest writes
    tracks and returns stable_ids [else stop]."""
    if not ingest_cfg.BATCH_RE.match(batch):
        raise HTTPException(
            422, f"invalid batch name {batch!r} (need {ingest_cfg.BATCH_RE.pattern})"
        )
    dest_dir = (ingest_cfg.INGEST_INBOX / batch).resolve()
    if not dest_dir.is_dir():
        raise HTTPException(404, f"batch {batch!r} not found")

    staged = _staged_audio_files(dest_dir)
    if not staged:
        return MaterializeOut(batch=batch, tracks=[])

    db_path = Path(request.app.state.state_db_path)
    if not db_path.is_file():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "state_db_missing",
                "message": f"state DB not found at {db_path}",
            },
        )

    conn = state_db.open_rw(db_path)
    try:
        before_ids = {
            row[0]
            for row in conn.execute(
                "SELECT stable_id FROM tracks WHERE deleted_at IS NULL"
            ).fetchall()
        }
        conn.execute("BEGIN IMMEDIATE")
        with StateWriter(conn, actor="webui") as writer:
            # The user picked these files to add, so one they removed earlier
            # comes back rather than failing the drop (LIBM-141).
            folder_ingest.ingest_folder(
                writer, [dest_dir], dry_run=False, restore_removed=True
            )

        # Commit only after EVERY staged path resolved (see _resolve_staged).
        tracks = _resolve_staged(conn, dest_dir, staged, before_ids)
        conn.commit()
        return MaterializeOut(batch=batch, tracks=tracks)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
