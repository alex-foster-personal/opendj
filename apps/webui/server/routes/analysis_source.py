"""Per-lane analysis source selection over HTTP (PARITY-02, agent parity).

``GET  /api/v1/analysis/source`` reports, per selection lane, the persisted
default, the in-memory dev toggle, and the resulting effective source.
``PUT  /api/v1/analysis/source`` sets either half.

Agent-native parity is the reason this exists as an endpoint rather than a
UI control with a module-level variable behind it: every option in the
top-left dropdown must be settable and readable by an agent without
touching the UI, and the path must appear in ``apps/webui/openapi.json``.

The toggle is PROCESS-LOCAL by design (it is in-memory, spec section 3),
so this endpoint is also the only correct writer for it. The CLI
(:mod:`apps.analysis.selection_cli`) is a client of this endpoint, never a
second writer: a CLI that mutated its own import of the module would leave
the running server on the old state and report success.

This router writes ONLY to ``analysis_source_default``. It never touches
``track_fields``.

-Claude
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from apps.analysis import selection as sel
from apps.shared.paths import STATE_DB

router = APIRouter(prefix="/analysis", tags=["analysis"])


class LaneSourceOut(BaseModel):
    """One lane's persisted default, dev toggle, and resulting source."""

    default: str = Field(description="Persisted per-lane default source: rbx or own")
    toggle: str = Field(
        description="In-memory dev toggle: unset, rbx or own. Launches unset."
    )
    effective: str = Field(
        description="The source actually read: the toggle unless it is unset"
    )


class AnalysisSourceOut(BaseModel):
    lanes: dict[str, LaneSourceOut]


class AnalysisSourcePut(BaseModel):
    """Set the default, the toggle, or both, for one lane."""

    lane: str = Field(description="beatgrid, key, waveform, loudness or vocal")
    default: str | None = Field(
        default=None, description="rbx or own. Persisted; survives a relaunch."
    )
    toggle: str | None = Field(
        default=None,
        description="unset, rbx or own. In-memory; resets to unset on relaunch.",
    )


def _db_path(request: Request) -> Path:
    return Path(getattr(request.app.state, "analysis_db_path", STATE_DB))


def _open(request: Request) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(request))
    sel.ensure_tables(conn)
    return conn


@router.get("/source", response_model=AnalysisSourceOut)
def get_analysis_source(request: Request) -> AnalysisSourceOut:
    conn = _open(request)
    try:
        return AnalysisSourceOut(**sel.source_state(conn))
    finally:
        conn.close()


@router.put("/source", response_model=AnalysisSourceOut)
def put_analysis_source(request: Request, body: AnalysisSourcePut) -> AnalysisSourceOut:
    if body.default is None and body.toggle is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_change_requested",
                "message": "PUT /analysis/source needs at least one of default or toggle",
            },
        )
    conn = _open(request)
    try:
        if body.default is not None:
            sel.set_default(conn, body.lane, body.default)
            conn.commit()
        if body.toggle is not None:
            sel.set_toggle(body.lane, body.toggle)
        return AnalysisSourceOut(**sel.source_state(conn))
    except sel.SelectionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_selection", "message": str(exc)},
        ) from exc
    finally:
        conn.close()


__all__ = ["AnalysisSourceOut", "AnalysisSourcePut", "LaneSourceOut", "router"]
