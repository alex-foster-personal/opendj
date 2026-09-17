"""HTTP routes for the performance rescue snapshot ring."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status

from apps.engine_core.config import EngineConfig
from apps.engine_core.rescue.models import (
    RescueDeckOutcomeOut,
    RescueRestoreIn,
    RescueRestoreOut,
    RescueSnapshotMeta,
    RescueSnapshotsOut,
)
from apps.engine_core.rescue.restore import (
    RescueRestoreError,
    compute_deck_outcomes,
    resolve_restore_mode,
    snapshot_age_ms,
)
from apps.engine_core.rescue.store import RescueStore
from apps.shared.state import db as state_db

SNAPSHOTS_PATH = "/api/v1/rescue/snapshots"
RESTORE_PATH = "/api/v1/rescue/restore"


def _store_for_request(request: Request) -> RescueStore:
    cfg = getattr(request.app.state, "engine_cfg", None)
    if not isinstance(cfg, EngineConfig):
        raise HTTPException(status_code=503, detail="engine config unavailable")
    return RescueStore(cfg.data_dir)


def _present_stable_ids(data_dir: Path) -> set[str] | None:
    db_path = data_dir / "state" / "state.db"
    if not db_path.is_file():
        return None
    try:
        conn = state_db.open_ro(db_path)
        try:
            # A soft-deleted track is gone from the user's library, so a restore
            # must report it missing rather than reload it.
            rows = conn.execute(
                "SELECT stable_id FROM tracks WHERE deleted_at IS NULL"
            ).fetchall()
            return {str(row[0]) for row in rows}
        finally:
            conn.close()
    except Exception:
        return None


def add_rescue_routes(app: FastAPI) -> None:
    @app.get(
        SNAPSHOTS_PATH,
        response_model=RescueSnapshotsOut,
        tags=["performance"],
        name="rescue_snapshots_list",
    )
    def list_rescue_snapshots(request: Request) -> RescueSnapshotsOut:
        store = _store_for_request(request)
        now_ms = int(time.time() * 1000)
        snapshots = [
            RescueSnapshotMeta.model_validate(row)
            for row in store.list_metadata(now_ms=now_ms)
        ]
        return RescueSnapshotsOut(snapshots=snapshots)

    @app.post(
        RESTORE_PATH,
        response_model=RescueRestoreOut,
        tags=["performance"],
        name="rescue_restore",
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "description": "library unreadable (state/state.db missing, locked, or corrupt)"
            }
        },
    )
    def restore_rescue_snapshot(
        body: RescueRestoreIn, request: Request
    ) -> RescueRestoreOut:
        store = _store_for_request(request)
        now_ms = int(time.time() * 1000)
        entry = store.get_entry(body.snapshot_id, now_ms=now_ms)
        if entry is None:
            if body.snapshot_id is not None:
                raise HTTPException(status_code=404, detail="snapshot not found")
            raise HTTPException(status_code=422, detail="rescue ring is empty")
        age_ms = snapshot_age_ms(captured_at_ms=entry.captured_at_ms, now_ms=now_ms)
        try:
            mode = resolve_restore_mode(age_ms=age_ms, play=body.play)
        except RescueRestoreError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        cfg = request.app.state.engine_cfg
        present_stable_ids = _present_stable_ids(cfg.data_dir)
        if present_stable_ids is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="library_unreadable: cannot read state/state.db",
            )
        raw_outcomes = compute_deck_outcomes(
            entry.payload,
            mode=mode,
            present_stable_ids=present_stable_ids,
        )
        return RescueRestoreOut(
            snapshot_id=entry.id,
            captured_at_ms=entry.captured_at_ms,
            mode=mode,
            payload=entry.payload,
            decks={
                deck_id: RescueDeckOutcomeOut(**outcome)
                for deck_id, outcome in raw_outcomes.items()
            },
        )


__all__ = [
    "RESTORE_PATH",
    "SNAPSHOTS_PATH",
    "add_rescue_routes",
]
