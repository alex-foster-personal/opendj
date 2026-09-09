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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from apps.analysis import selection as sel
from apps.analysis import store as analysis_store
from apps.shared.paths import STATE_DB

from ..backend import InMemoryBackend, StateBackend
from ..deps import get_write_state

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
    expected_toggle: str | None = Field(
        default=None,
        description=(
            "Compare-and-set precondition for `toggle`: apply it only if the "
            "lane's CURRENT toggle equals this value, atomically. 409 on a "
            "mismatch. Ignored unless `toggle` is also given; a plain "
            "`toggle` with no `expected_toggle` sets unconditionally, exactly "
            "as before this field existed."
        ),
    )


def _db_path(request: Request) -> Path:
    return Path(getattr(request.app.state, "analysis_db_path", STATE_DB))


def _open_ro(request: Request) -> sqlite3.Connection:
    """Read-only. A GET must not migrate, and must not need the writer lock.

    `store.open_conn` opens READ-WRITE and runs the shared-state migrations
    plus the analysis DDL, so using it for a GET made a nominal read mutate
    `state.db` -- from a host that may not hold the writer lock, and during a
    lock-probe outage, both of which `deps.py` exists to exclude (Codex P1,
    PR #1549). `get_default` already answers correctly when the table is
    absent, so the read needs no schema work at all.

    `query_only` on top of `mode=ro` so a write is refused by the connection
    rather than by the filesystem: a `state.db` on a writable volume would
    otherwise still accept one.
    """
    path = _db_path(request)
    if not path.exists():
        # `mode=ro` raises on an absent file, so a GET on a fresh daemon --
        # which is exactly when `make_backend()` chose InMemoryBackend
        # BECAUSE state.db does not exist -- would 500 instead of answering
        # with the documented rbx defaults (Codex P2, PR #1549). An in-memory
        # database is the honest stand-in: no table, so `get_default` returns
        # `rbx` for every lane, which is the true answer, and nothing is
        # created on disk.
        return sqlite3.connect(":memory:")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _open(request: Request) -> sqlite3.Connection:
    """Open with the FULL analysis schema, not just the selection table.

    Promoting a lane to own on a database with no `analysis_projection` was
    a real defect (Codex P1, PR #1549): the readers are hardened against it
    now, but the honest fix is that the surface which can say `own` also
    guarantees the store that word refers to. `store.open_conn` runs the
    Phase 5 migrations and the analysis DDL, so a PUT can never leave the
    database in a state its own GET describes wrongly.
    """
    return analysis_store.open_conn(_db_path(request))


def _require_persistent_backend(request: Request) -> None:
    """Refuse to persist a default the serving backend will not read.

    The in-memory backend has no `track_fields` and no projection to read, so
    a persisted `own` would be invisible to it. The DEV TOGGLE is exempt: it
    is process-local and explicitly a testing affordance, so setting it on a
    backend that cannot honour it costs nothing and resets on relaunch.
    """
    backend = getattr(request.app.state, "backend", None)
    if isinstance(backend, InMemoryBackend):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "no_persistent_library",
                "message": (
                    "this daemon is running on the in-memory backend, which has "
                    "no state.db to read an own analysis from, so persisting a "
                    "lane default here would report `own` while /tracks kept "
                    "serving rekordbox values. Start the daemon against a real "
                    "data dir first."
                ),
            },
        )


@router.get("/source", response_model=AnalysisSourceOut)
def get_analysis_source(request: Request) -> AnalysisSourceOut:
    conn = _open_ro(request)
    try:
        return AnalysisSourceOut(**sel.source_state(conn))
    finally:
        conn.close()


@router.put("/source", response_model=AnalysisSourceOut)
def put_analysis_source(
    request: Request,
    body: AnalysisSourcePut,
    # Cross-host write exclusion. This route opens state.db READ-WRITE, so it
    # is a mutating route and has to answer to the same lock as every other
    # one: 503 when another host holds the writer lock, and 503 when the lock
    # PROBE itself failed (deps.py fails closed there). Without the
    # dependency it would persist a promotion straight through both states
    # (Codex P1, PR #1549). The returned backend is unused -- the guard is
    # the point.
    _write_guard: StateBackend = Depends(get_write_state),
) -> AnalysisSourceOut:
    if body.default is None and body.toggle is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_change_requested",
                "message": "PUT /analysis/source needs at least one of default or toggle",
            },
        )
    # Validate EVERYTHING before mutating ANYTHING. A PUT carrying a valid
    # default and an invalid toggle used to persist the default and then
    # return 422, which a client reads as "nothing happened" (Codex P2).
    try:
        sel.check_lane(body.lane)
        if body.default is not None:
            sel.check_source(body.default)
        if body.toggle is not None:
            sel.check_toggle_state(body.toggle)
        if body.expected_toggle is not None:
            sel.check_toggle_state(body.expected_toggle)
    except sel.SelectionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_selection", "message": str(exc)},
        ) from exc

    # A promotion the RUNNING backend cannot serve is a lie, not a setting.
    # The daemon falls back to InMemoryBackend when `state.db` does not exist
    # (app.py: make_backend()), and this endpoint would then create a fresh
    # database, persist `own` into it, and report `effective: own` while
    # /tracks kept serving the in-memory library until restart (Codex P1).
    # Refused rather than silently migrated: swapping app.state.backend under
    # a live request would change what every in-flight reader is talking to.
    if body.default is not None:
        _require_persistent_backend(request)

    # A toggle-only PUT must not TOUCH the disk. The toggle is process-local
    # by definition, and opening writably would create state.db -- after
    # which `make_backend()` picks SqliteBackend on the NEXT launch purely
    # because the file exists, so a session-only developer toggle would
    # permanently replace an in-memory library with an empty SQLite one
    # (Codex P2, PR #1549). Only a persisted default earns a writable open.
    conn = _open(request) if body.default is not None else _open_ro(request)
    try:
        if body.default is not None:
            sel.set_default(conn, body.lane, body.default)
            conn.commit()
        if body.toggle is not None:
            if body.expected_toggle is not None:
                if not sel.compare_and_set_toggle(
                    body.lane, body.expected_toggle, body.toggle
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "toggle_changed",
                            "message": (
                                f"lane {body.lane!r}'s toggle no longer holds "
                                f"{body.expected_toggle!r}; someone else changed "
                                "it since, so this compare-and-set was refused "
                                "rather than overwriting a newer value"
                            ),
                        },
                    )
            else:
                sel.set_toggle(body.lane, body.toggle)
        return AnalysisSourceOut(**sel.source_state(conn))
    finally:
        conn.close()


__all__ = ["AnalysisSourceOut", "AnalysisSourcePut", "LaneSourceOut", "router"]
