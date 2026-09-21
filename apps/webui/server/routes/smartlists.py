"""Smartlists router -- read/evaluate plus CAS rule replacement.

Evaluation and compare-and-swap rule-write surface over the ``smartlists``
table + Phase 5 shared-state schema. The rule-AST -> SQL compiler in
:mod:`apps.smartlists.evaluator` is reused AS-IS; no materialiser calls.
The rule editor and agents share this router's contract:

  * ``GET  /api/v1/smartlists``                -- list summaries
    (id, name, rule AST + human-readable rule_summary, order_by, ...).
  * ``POST /api/v1/smartlists``                -- create (name + rule AST;
    ``order_by`` defaults to ``added_date desc``, matching the CLI).
  * ``GET  /api/v1/smartlists/{id}``           -- one summary.
  * ``PUT  /api/v1/smartlists/{id}``           -- complete rule replacement,
    requiring the detail response ETag through ``If-Match``.
  * ``DELETE /api/v1/smartlists/{id}``         -- tombstone a smartlist
    (``deleted_at`` set; row retained for recovery in principle).
  * ``POST /api/v1/smartlists/{id}/duplicate`` -- clone rule under a new id.
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
  * 409 ``conflict``                  -- stale ``If-Match``; current summary
    and ETag are returned without mutation.
  * 409 ``SMARTLIST_NAME_CONFLICT``   -- create reused an existing name.
  * 428 ``precondition_required``     -- update omitted ``If-Match``.

NOTE for the wave integrator: wire with
``app.include_router(smartlists_routes.router, prefix=api_prefix)`` in
apps/webui/server/app.py (hotspot -- not edited here).
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from apps.shared.events import publish
from apps.shared.pairings.schema_sql import migrate_smartlists_deleted_at
from apps.shared.smartlists import SmartlistRow, SmartlistRuleError
from apps.smartlists.evaluator import EvaluatorError, evaluate
from apps.smartlists.repo import (
    SmartlistRevisionConflict,
    SmartlistsRepo,
    SmartlistsRepoError,
    smartlist_revision,
)
from apps.webui.soft_deletes import has_soft_deletes

from .. import rb_vendor
from ..backend import ConflictError, StateBackend
from ..deps import get_read_state, get_write_state
from ..errors import precondition_required
from ..models import TrackRowOut
from .smartlists_models import (
    SmartlistConflictBody,
    SmartlistCreateIn,
    SmartlistDuplicateIn,
    SmartlistPreconditionRequiredBody,
    SmartlistSummary,
    SmartlistTracks,
    SmartlistUpdateIn,
)

router = APIRouter(prefix="/smartlists", tags=["smartlists"])

_COLS: str = (
    "id, name, rule, rule_schema_version, referenced_fields, order_by, "
    "last_evaluated_at, last_materialized_track_ids, created_at, modified_at"
)

_ETAG_RESPONSE_HEADER: dict[str, Any] = {
    "ETag": {
        "description": "Strong validator for the complete persisted smartlist row",
        "schema": {"type": "string"},
    },
}
_GET_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"headers": _ETAG_RESPONSE_HEADER},
}
_IF_MATCH_OPENAPI_PARAMETER: dict[str, Any] = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "description": "Smartlist ETag returned by GET /api/v1/smartlists/{smartlist_id}",
    "schema": {"type": "string"},
}
_POST_RESPONSES: dict[int | str, dict[str, Any]] = {
    201: {"headers": _ETAG_RESPONSE_HEADER},
}
_PUT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"headers": _ETAG_RESPONSE_HEADER},
    409: {
        "model": SmartlistConflictBody,
        "description": "If-Match does not match the current smartlist revision",
        "headers": _ETAG_RESPONSE_HEADER,
    },
    428: {
        "model": SmartlistPreconditionRequiredBody,
        "description": "If-Match is required for every smartlist update",
    },
}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _etag(revision: str) -> str:
    return f'"{revision}"'


def _revision_from_if_match(if_match: str) -> str:
    if (
        len(if_match) >= 2
        and if_match.startswith('"')
        and if_match.endswith('"')
    ):
        return if_match[1:-1]
    return f"invalid-if-match:{if_match}"


def _live_filter(conn: sqlite3.Connection) -> str:
    if not _table_exists(conn, "smartlists"):
        return ""
    if has_soft_deletes(conn, "smartlists"):
        return " AND (deleted_at IS NULL OR deleted_at = '')"
    return ""


def summarize_rule(rule: dict[str, Any]) -> str:
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


def _to_summary(
    row: SmartlistRow, *, count: int | None = None,
) -> SmartlistSummary:
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
        count=count,
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _row_to_model(row: tuple) -> SmartlistRow:
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
        created_at=_parse(row[8]) or datetime.fromtimestamp(0, UTC),
        modified_at=_parse(row[9]) or datetime.fromtimestamp(0, UTC),
        _raw_rule_json=row[2],
    )


def get_smartlists_conn(request: Request) -> Iterator[sqlite3.Connection]:
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
    live = _live_filter(conn)
    if _table_exists(conn, "smartlists"):
        raw = conn.execute(
            f"SELECT {_COLS} FROM smartlists WHERE id=?{live}",
            (smartlist_id,),
        ).fetchone()
    else:
        raw = None
    if raw is None:
        raise HTTPException(status_code=404, detail={
            "code": "SMARTLIST_NOT_FOUND",
            "message": f"smartlist not found: {smartlist_id}",
        })
    return _row_to_model(raw)


def _repo_error_to_http(exc: SmartlistsRepoError) -> HTTPException:
    if "already exists" in str(exc):
        return HTTPException(status_code=409, detail={
            "code": "SMARTLIST_NAME_CONFLICT",
            "message": str(exc),
        })
    return HTTPException(status_code=422, detail={
        "code": "SMARTLIST_RULE_INVALID",
        "message": str(exc),
    })


@router.get("", response_model=list[SmartlistSummary])
def list_smartlists(
    include_counts: bool = Query(
        False,
        description="Live-evaluate each smartlist membership count.",
    ),
    conn: sqlite3.Connection = Depends(get_smartlists_conn),  # noqa: B008  # FastAPI DI
) -> list[SmartlistSummary]:
    if not _table_exists(conn, "smartlists"):
        return []
    live = _live_filter(conn)
    raw_rows = conn.execute(
        f"SELECT {_COLS} FROM smartlists WHERE 1=1{live} ORDER BY name"
    ).fetchall()
    rows = [_row_to_model(raw) for raw in raw_rows]
    if not include_counts:
        return [_to_summary(row) for row in rows]

    summaries: list[SmartlistSummary] = []
    for row in rows:
        try:
            count = len(evaluate(row.rule, conn, order_by=row.order_by))
        except (SmartlistRuleError, EvaluatorError):
            count = None
        summaries.append(_to_summary(row, count=count))
    return summaries


@router.post(
    "",
    response_model=SmartlistSummary,
    status_code=status.HTTP_201_CREATED,
    responses=_POST_RESPONSES,
)
def create_smartlist(
    body: SmartlistCreateIn,
    response: Response,
    _backend: Annotated[StateBackend, Depends(get_write_state)],
    conn: Annotated[sqlite3.Connection, Depends(get_smartlists_write_conn)],
) -> SmartlistSummary:
    try:
        row = SmartlistsRepo(conn).create(
            body.name,
            body.rule,
            order_by=body.order_by or "added_date desc",
        )
    except SmartlistRuleError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "SMARTLIST_RULE_INVALID",
            "message": str(exc),
        }) from exc
    except SmartlistsRepoError as exc:
        raise _repo_error_to_http(exc) from exc
    response.headers["ETag"] = _etag(smartlist_revision(row))
    publish("library.changed", {"kind": "smartlists", "ids": [row.id]})
    return _to_summary(row)


@router.get(
    "/{smartlist_id}",
    response_model=SmartlistSummary,
    responses=_GET_RESPONSES,
)
def get_smartlist(
    smartlist_id: str,
    response: Response,
    conn: sqlite3.Connection = Depends(get_smartlists_conn),  # noqa: B008  # FastAPI DI
) -> SmartlistSummary:
    row = _fetch_smartlist(conn, smartlist_id)
    response.headers["ETag"] = _etag(smartlist_revision(row))
    return _to_summary(row)


@router.put(
    "/{smartlist_id}",
    response_model=SmartlistSummary,
    responses=_PUT_RESPONSES,
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
)
def update_smartlist(
    smartlist_id: str,
    body: SmartlistUpdateIn,
    request: Request,
    response: Response,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
    conn: sqlite3.Connection = Depends(get_smartlists_write_conn),  # noqa: B008  # FastAPI DI
) -> SmartlistSummary | Response:
    if_match = request.headers.get("If-Match")
    if if_match is None:
        return precondition_required(
            "PUT /smartlists/{smartlist_id} requires If-Match header"
        )
    _fetch_smartlist(conn, smartlist_id)
    migrate_smartlists_deleted_at(conn)
    try:
        row = SmartlistsRepo(conn, ensure_schema=False).update_rule(
            smartlist_id,
            body.rule,
            expected_revision=_revision_from_if_match(if_match),
            order_by=body.order_by,
            name=body.name,
        )
    except SmartlistRevisionConflict as exc:
        current_etag = _etag(exc.current_revision)
        raise ConflictError(
            _to_summary(exc.current).model_dump(mode="json"),
            current_etag,
        ) from exc
    except SmartlistRuleError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "SMARTLIST_RULE_INVALID",
            "message": str(exc),
        }) from exc
    except SmartlistsRepoError as exc:
        raise _repo_error_to_http(exc) from exc
    response.headers["ETag"] = _etag(smartlist_revision(row))
    publish("library.changed", {"kind": "smartlists", "ids": [smartlist_id]})
    return _to_summary(row)


@router.delete(
    "/{smartlist_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_smartlist(
    smartlist_id: str,
    _backend: Annotated[StateBackend, Depends(get_write_state)],
    conn: Annotated[sqlite3.Connection, Depends(get_smartlists_write_conn)],
) -> Response:
    """Tombstone a smartlist row (soft delete via SmartlistsRepo.delete)."""
    if not _table_exists(conn, "smartlists"):
        raise HTTPException(status_code=404, detail={
            "code": "SMARTLIST_NOT_FOUND",
            "message": f"smartlist not found: {smartlist_id}",
        })
    migrate_smartlists_deleted_at(conn)
    removed = SmartlistsRepo(conn, ensure_schema=False).delete(smartlist_id)
    if not removed:
        raise HTTPException(status_code=404, detail={
            "code": "SMARTLIST_NOT_FOUND",
            "message": f"smartlist not found: {smartlist_id}",
        })
    publish("library.changed", {"kind": "smartlists", "ids": [smartlist_id]})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{smartlist_id}/duplicate",
    response_model=SmartlistSummary,
    status_code=status.HTTP_201_CREATED,
    responses=_POST_RESPONSES,
)
def duplicate_smartlist(
    smartlist_id: str,
    response: Response,
    _backend: Annotated[StateBackend, Depends(get_write_state)],
    conn: Annotated[sqlite3.Connection, Depends(get_smartlists_write_conn)],
    body: SmartlistDuplicateIn | None = None,
) -> SmartlistSummary:
    _fetch_smartlist(conn, smartlist_id)
    migrate_smartlists_deleted_at(conn)
    try:
        row = SmartlistsRepo(conn, ensure_schema=False).duplicate(
            smartlist_id,
            name=body.name if body is not None else None,
        )
    except SmartlistsRepoError as exc:
        if "not found" in str(exc):
            raise HTTPException(status_code=404, detail={
                "code": "SMARTLIST_NOT_FOUND",
                "message": str(exc),
            }) from exc
        raise _repo_error_to_http(exc) from exc
    response.headers["ETag"] = _etag(smartlist_revision(row))
    publish("library.changed", {"kind": "smartlists", "ids": [row.id]})
    return _to_summary(row)


@router.get("/{smartlist_id}/tracks", response_model=SmartlistTracks)
def get_smartlist_tracks(
    smartlist_id: str,
    limit: int | None = Query(
        None, ge=1, le=10000,
        description="Cap the evaluated membership (evaluator LIMIT).",
    ),
    conn: sqlite3.Connection = Depends(get_smartlists_conn),  # noqa: B008  # FastAPI DI
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> SmartlistTracks:
    row = _fetch_smartlist(conn, smartlist_id)
    try:
        stable_ids = evaluate(
            row.rule, conn, order_by=row.order_by, limit=limit,
        )
    except SmartlistRuleError as exc:
        raise HTTPException(status_code=500, detail={
            "code": "SMARTLIST_RULE_INVALID",
            "message": f"smartlist {smartlist_id}: {exc}",
        }) from exc
    except EvaluatorError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "SMARTLIST_EVAL_UNAVAILABLE",
            "message": str(exc),
        }) from exc

    tracks_map = backend.get_tracks_bulk(stable_ids)
    missing = [sid for sid in stable_ids if sid not in tracks_map]
    if missing:
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
    "SmartlistCreateIn",
    "SmartlistDuplicateIn",
    "SmartlistSummary",
    "SmartlistTracks",
    "SmartlistUpdateIn",
    "get_smartlists_conn",
    "get_smartlists_write_conn",
    "router",
    "summarize_rule",
]
