"""Smartlists router -- read/evaluate plus full rule replacement.

Read-only evaluation surface over the ``smartlists`` table + Phase 5
shared-state schema. The rule-AST -> SQL compiler in
:mod:`apps.smartlists.evaluator` is reused AS-IS; no materialiser calls,
no writes. Downstream (rule-editor-ui) builds CRUD against this router's
contract:

  * ``GET  /api/v1/smartlists``                -- list summaries
    (id, name, rule AST + human-readable rule_summary, order_by, ...).
  * ``GET  /api/v1/smartlists/{id}``           -- one summary.
  * ``GET  /api/v1/smartlists/{id}/tracks``    -- live evaluation:
    ordered ``items`` (stable_ids) + hydrated ``tracks`` rows shaped
    exactly like playlist detail (:class:`..models.TrackRowOut`).

Error contract (explicit ``{"detail": {code, message}}`` like the rest of
the daemon):

  * 503 ``SMARTLISTS_DB_UNAVAILABLE`` -- no state.db on disk (InMemory
    deployments have no smartlists surface; we never invent one).
  * 404 ``SMARTLIST_NOT_FOUND``       -- unknown id.
  * 503 ``SMARTLIST_EVAL_UNAVAILABLE``-- Phase 5 tracks tables missing.
  * 500 ``SMARTLIST_RULE_INVALID``    -- stored rule fails validation
    (corrupt row; fail loudly, never skip predicates).
  * 500 ``SMARTLIST_MEMBER_MISSING``  -- evaluator returned stable_ids
    the backend cannot hydrate (state/backend divergence).

NOTE for the wave integrator: wire with
``app.include_router(smartlists_routes.router, prefix=api_prefix)`` in
apps/webui/server/app.py (hotspot -- not edited here).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from apps.shared.smartlists import SmartlistRow, SmartlistRuleError
from apps.smartlists.evaluator import EvaluatorError, evaluate
from apps.smartlists.repo import SmartlistsRepo, SmartlistsRepoError

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from ..models import TrackRowOut

router = APIRouter(prefix="/smartlists", tags=["smartlists"])

# Columns mirror apps/smartlists/repo.py _COLS -- kept local so this
# router never constructs the repo in write mode (ensure_schema would
# CREATE TABLE, which a query_only connection rightly refuses).
_COLS: str = (
    "id, name, rule, rule_schema_version, referenced_fields, order_by, "
    "last_evaluated_at, last_materialized_track_ids, created_at, modified_at"
)


# ----------------------------------------------------------- schemas


class SmartlistSummary(BaseModel):
    """List/detail row for one smartlist (contract for rule-editor-ui)."""

    id: str
    name: str
    # Full rule AST (predicate {field, op, value} / logical {op, children}).
    rule: dict[str, Any]
    # One-line human-readable rendering of the AST, e.g.
    # "(bpm between [120, 130] AND genre contains \"techno\")".
    rule_summary: str
    order_by: str
    referenced_fields: list[str]
    rule_schema_version: int
    last_evaluated_at: str | None
    created_at: str
    modified_at: str


class SmartlistUpdateIn(BaseModel):
    """Complete desired rule plus optional replacement ordering."""

    rule: dict[str, Any]
    order_by: str | None = None


class SmartlistTracks(BaseModel):
    """Live evaluation result, shaped like playlist detail.

    ``items`` is the full ordered membership (stable_ids, evaluator
    order); ``tracks`` are hydrated rows in the SAME order, field-for-
    field identical to the playlist-detail ``tracks`` rows so browser
    table components render either without branching.
    """

    smartlist_id: str
    name: str
    rule_summary: str
    order_by: str
    items: list[str]
    tracks: list[TrackRowOut]


# ----------------------------------------------------------- _helpers


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def summarize_rule(rule: dict[str, Any]) -> str:
    """One-line human rendering of a rule AST (tree tooltips / list rows)."""
    if "field" in rule:
        return (
            f"{rule['field']} {rule['op']} "
            f"{json.dumps(rule['value'], sort_keys=True)}"
        )
    op = rule["op"]
    if op == "not":
        return "NOT " + summarize_rule(rule["children"][0])
    joined = f" {op.upper()} ".join(
        summarize_rule(c) for c in rule["children"]
    )
    return f"({joined})"


def _to_summary(row: SmartlistRow) -> SmartlistSummary:
    return SmartlistSummary(
        id=row.id,
        name=row.name,
        rule=row.rule,
        rule_summary=summarize_rule(row.rule),
        order_by=row.order_by,
        referenced_fields=sorted(row.referenced_fields),
        rule_schema_version=row.rule_schema_version,
        last_evaluated_at=_iso(row.last_evaluated_at),
        created_at=_iso(row.created_at) or "",
        modified_at=_iso(row.modified_at) or "",
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _row_to_model(row: tuple) -> SmartlistRow:
    """Project a ``smartlists`` row (repo.py column order) read-only."""

    def _parse(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    return SmartlistRow(
        id=row[0],
        name=row[1],
        rule=json.loads(row[2]),
        rule_schema_version=row[3],
        referenced_fields=frozenset(json.loads(row[4])),
        order_by=row[5],
        last_evaluated_at=_parse(row[6]),
        last_materialized_track_ids=json.loads(row[7]) if row[7] else [],
        created_at=_parse(row[8]) or datetime.fromtimestamp(0, timezone.utc),
        modified_at=_parse(row[9]) or datetime.fromtimestamp(0, timezone.utc),
        _raw_rule_json=row[2],
    )


def get_smartlists_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Read-only sqlite conn on the daemon's state.db (query_only guard).

    503s explicitly when state.db is absent -- an InMemoryBackend deploy
    has no smartlists store and we never mock one.

    check_same_thread=False (unlike state_db.open_ro): FastAPI runs sync
    dependencies and sync endpoints on DIFFERENT threadpool threads, so a
    conn created here with the sqlite default raises ProgrammingError in
    the endpoint under load. Read-only + query_only + per-request scope
    makes cross-thread use safe (found live in the e2e-gating round).
    """
    db_path = Path(
        getattr(request.app.state, "state_db_path", "data/state/state.db")
    )
    if not db_path.exists():
        raise HTTPException(status_code=503, detail={
            "code": "SMARTLISTS_DB_UNAVAILABLE",
            "message": (
                f"state DB not found at {db_path}; run "
                "`python -m apps.shared.state.cli init` first."
            ),
        })
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, isolation_level=None,
        check_same_thread=False,
    )
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def get_smartlists_write_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Writable, autocommit connection for explicit smartlist updates."""
    db_path = Path(
        getattr(request.app.state, "state_db_path", "data/state/state.db")
    )
    if not db_path.exists():
        raise HTTPException(status_code=503, detail={
            "code": "SMARTLISTS_DB_UNAVAILABLE",
            "message": f"state DB not found at {db_path}",
        })
    conn = sqlite3.connect(
        str(db_path), isolation_level=None, check_same_thread=False,
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()


def _fetch_smartlist(
    conn: sqlite3.Connection, smartlist_id: str,
) -> SmartlistRow:
    if _table_exists(conn, "smartlists"):
        raw = conn.execute(
            f"SELECT {_COLS} FROM smartlists WHERE id=?", (smartlist_id,),
        ).fetchone()
    else:
        raw = None
    if raw is None:
        raise HTTPException(status_code=404, detail={
            "code": "SMARTLIST_NOT_FOUND",
            "message": f"smartlist not found: {smartlist_id}",
        })
    return _row_to_model(raw)


# ----------------------------------------------------------- endpoints


@router.get("", response_model=list[SmartlistSummary])
def list_smartlists(
    conn: sqlite3.Connection = Depends(get_smartlists_conn),
) -> list[SmartlistSummary]:
    # The smartlists table is created lazily by the first CRUD write
    # (ensure_phase08_tables). On a state.db that predates Phase 08 its
    # absence IS the true state -- zero smartlists defined -- not a
    # failure to mask, so an empty list is the honest answer.
    if not _table_exists(conn, "smartlists"):
        return []
    rows = conn.execute(
        f"SELECT {_COLS} FROM smartlists ORDER BY name"
    ).fetchall()
    return [_to_summary(_row_to_model(r)) for r in rows]


@router.get("/{smartlist_id}", response_model=SmartlistSummary)
def get_smartlist(
    smartlist_id: str,
    conn: sqlite3.Connection = Depends(get_smartlists_conn),
) -> SmartlistSummary:
    return _to_summary(_fetch_smartlist(conn, smartlist_id))


@router.put("/{smartlist_id}", response_model=SmartlistSummary)
def update_smartlist(
    smartlist_id: str,
    body: SmartlistUpdateIn,
    conn: sqlite3.Connection = Depends(get_smartlists_write_conn),
) -> SmartlistSummary:
    """Apply a complete rule replacement and return persisted readback."""
    _fetch_smartlist(conn, smartlist_id)
    try:
        row = SmartlistsRepo(conn, ensure_schema=False).update_rule(
            smartlist_id, body.rule, order_by=body.order_by,
        )
    except (SmartlistRuleError, SmartlistsRepoError) as exc:
        raise HTTPException(status_code=422, detail={
            "code": "SMARTLIST_RULE_INVALID",
            "message": str(exc),
        }) from exc
    return _to_summary(row)


@router.get("/{smartlist_id}/tracks", response_model=SmartlistTracks)
def get_smartlist_tracks(
    smartlist_id: str,
    limit: int | None = Query(
        None, ge=1, le=10000,
        description="Cap the evaluated membership (evaluator LIMIT).",
    ),
    conn: sqlite3.Connection = Depends(get_smartlists_conn),
    backend: StateBackend = Depends(get_read_state),
) -> SmartlistTracks:
    row = _fetch_smartlist(conn, smartlist_id)
    try:
        stable_ids = evaluate(
            row.rule, conn, order_by=row.order_by, limit=limit,
        )
    except SmartlistRuleError as exc:
        # Stored rule no longer validates = corrupt row. Fail loudly;
        # never evaluate a partial rule.
        raise HTTPException(status_code=500, detail={
            "code": "SMARTLIST_RULE_INVALID",
            "message": f"smartlist {smartlist_id}: {exc}",
        }) from exc
    except EvaluatorError as exc:
        # Phase 5 tracks/track_fields tables missing (pre-ingest DB).
        raise HTTPException(status_code=503, detail={
            "code": "SMARTLIST_EVAL_UNAVAILABLE",
            "message": str(exc),
        }) from exc

    tracks_map = backend.get_tracks_bulk(stable_ids)
    missing = [sid for sid in stable_ids if sid not in tracks_map]
    if missing:
        # Evaluator ids the backend cannot hydrate = state/backend
        # divergence; fail loudly, never render invented rows.
        raise HTTPException(status_code=500, detail={
            "code": "SMARTLIST_MEMBER_MISSING",
            "message": (
                f"smartlist {smartlist_id} evaluated to {len(missing)} "
                f"stable_ids with no track row (first: {missing[:5]})"
            ),
        })
    rows = rb_vendor.build_track_rows([tracks_map[sid] for sid in stable_ids])
    return SmartlistTracks(
        smartlist_id=row.id,
        name=row.name,
        rule_summary=summarize_rule(row.rule),
        order_by=row.order_by,
        items=list(stable_ids),
        tracks=[TrackRowOut(**r) for r in rows],
    )


__all__ = [
    "SmartlistSummary",

    "SmartlistUpdateIn",
    "SmartlistTracks",
    "get_smartlists_conn",
    "get_smartlists_write_conn",
    "router",
    "summarize_rule",
]
