"""Admin SQL playground -- read-only queries against the daemon's state.db.

Requirements (mini-PRD):
  POST /admin/sql-query: run one read-only SQL statement against state.db.
Acceptance:
  [if] state.db is missing [then] 503 sql_db_unavailable
  [if] SQL is empty, multi-statement, or not read-only [then] 400 with named code
  [if] a SELECT succeeds [then] columns, rows, truncated, row_count
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/admin", tags=["admin"])

READONLY_KEYWORDS = frozenset({"SELECT", "WITH", "VALUES", "EXPLAIN"})
_ALLOWED_AUTH = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
})
_QUERY_TIMEOUT_S = 2.0


class SqlPlaygroundError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SqlQueryIn(BaseModel):
    sql: str
    limit: int = Field(default=200, ge=1, le=500)


class SqlQueryOut(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    row_count: int


def normalize_sql(sql: str) -> str:
    """Strip, drop a single trailing semicolon, refuse extra semicolons."""
    stripped = sql.strip()
    if not stripped:
        return stripped
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if ";" in stripped:
        raise SqlPlaygroundError("sql_invalid", "multiple SQL statements are not allowed")
    return stripped


def _strip_comments_for_keyword(sql: str) -> str:
    """Strip SQL comments only to locate the first keyword."""
    result: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        if sql[i : i + 2] == "--":
            while i < n and sql[i] != "\n":
                i += 1
        elif sql[i : i + 2] == "/*":
            end = sql.find("*/", i + 2)
            if end == -1:
                i = n
            else:
                i = end + 2
        else:
            result.append(sql[i])
            i += 1
    return "".join(result)


def assert_readonly_sql(sql: str) -> str:
    """Return the single statement, or raise SqlPlaygroundError(code=sql_invalid)."""
    normalized = normalize_sql(sql)
    if not normalized:
        raise SqlPlaygroundError("sql_invalid", "SQL is empty")
    keyword_source = _strip_comments_for_keyword(normalized)
    tokens = keyword_source.split()
    if not tokens:
        raise SqlPlaygroundError("sql_invalid", "SQL is empty or comments only")
    first = tokens[0].upper()
    if first not in READONLY_KEYWORDS:
        raise SqlPlaygroundError(
            "sql_invalid",
            f"first SQL keyword must be one of {sorted(READONLY_KEYWORDS)}",
        )
    return normalized


def _encode_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _readonly_authorizer(
    action: int,
    _arg1: str | None,
    _arg2: str | None,
    _dbname: str | None,
    _trigger_or_view: str | None,
) -> int:
    if action in _ALLOWED_AUTH:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _db_path(request: Request) -> Path:
    return Path(getattr(request.app.state, "state_db_path", "data/state/state.db"))


def _raise_http(code: str, message: str, status: int) -> None:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def execute_sql_query(db_path: Path, sql: str, limit: int) -> SqlQueryOut:
    if not db_path.exists():
        raise SqlPlaygroundError(
            "sql_db_unavailable",
            f"state.db not found at {db_path}; run `python -m apps.shared.state.cli init` first.",
        )
    normalized = assert_readonly_sql(sql)
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        isolation_level=None,
        check_same_thread=False,
    )
    deadline = time.monotonic() + _QUERY_TIMEOUT_S

    def progress_handler() -> int:
        if time.monotonic() > deadline:
            return 1
        return 0

    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_readonly_authorizer)
        conn.set_progress_handler(progress_handler, 1000)
        try:
            cursor = conn.execute(normalized)
        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "not authorized" in message.lower() or "query_only" in message.lower():
                raise SqlPlaygroundError("sql_not_readonly", message) from exc
            if "interrupted" in message.lower():
                raise SqlPlaygroundError("sql_query_timeout", "SQL query timed out") from exc
            raise SqlPlaygroundError("sql_query_failed", message) from exc
        except sqlite3.ProgrammingError as exc:
            raise SqlPlaygroundError("sql_query_failed", str(exc)) from exc

        columns = [desc[0] for desc in (cursor.description or ())]
        raw_rows = cursor.fetchmany(limit + 1)
        truncated = len(raw_rows) > limit
        rows_slice = raw_rows[:limit]
        rows = [[_encode_cell(cell) for cell in row] for row in rows_slice]
        return SqlQueryOut(
            columns=columns,
            rows=rows,
            truncated=truncated,
            row_count=len(rows_slice),
        )
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


@router.post("/sql-query", response_model=SqlQueryOut)
def sql_query(request: Request, body: SqlQueryIn) -> SqlQueryOut:
    db_path = _db_path(request)
    try:
        return execute_sql_query(db_path, body.sql, body.limit)
    except SqlPlaygroundError as exc:
        status = 503 if exc.code == "sql_db_unavailable" else 400
        _raise_http(exc.code, str(exc), status)
