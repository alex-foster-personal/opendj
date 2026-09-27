"""POST /api/v1/admin/sql-query.

Regression lines:
- SELECT against a temp state.db returns columns + rows
- missing DB is 503 sql_db_unavailable, never 200
- INSERT/UPDATE/DELETE/ATTACH is 400 sql_not_readonly (or sql_invalid) and unchanged
- multiple statements is 400 sql_invalid
- empty SQL is 400 sql_invalid
- limit truncates and sets truncated: true
- limit above 500 is 422 (FastAPI/Pydantic), not silently capped on the server after execute
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.webui.server.routes import sql_playground


def _seed_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO t (id, name) VALUES (1, 'a')")
    conn.commit()
    conn.close()


# REQ: ADMIN-02
def test_select_happy_path(client, tmp_path):
    db_path = tmp_path / "state.db"
    _seed_db(db_path)
    client.app.state.state_db_path = str(db_path)
    r = client.post("/api/v1/admin/sql-query", json={"sql": "SELECT id, name FROM t"})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["id", "name"]
    assert body["rows"] == [[1, "a"]]
    assert body["truncated"] is False
    assert body["row_count"] == 1


def test_missing_db_is_loud(client, tmp_path):
    client.app.state.state_db_path = str(tmp_path / "nope.db")
    r = client.post("/api/v1/admin/sql-query", json={"sql": "SELECT 1"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "sql_db_unavailable"


def test_insert_is_refused_and_table_unchanged(client, tmp_path):
    db_path = tmp_path / "state.db"
    _seed_db(db_path)
    client.app.state.state_db_path = str(db_path)
    r = client.post(
        "/api/v1/admin/sql-query",
        json={"sql": "INSERT INTO t (id, name) VALUES (2, 'b')"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] in {"sql_invalid", "sql_not_readonly"}
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert count == 1


def test_multiple_statements_is_invalid(client, tmp_path):
    db_path = tmp_path / "state.db"
    _seed_db(db_path)
    client.app.state.state_db_path = str(db_path)
    r = client.post("/api/v1/admin/sql-query", json={"sql": "SELECT 1; SELECT 2"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "sql_invalid"


def test_empty_sql_is_invalid(client, tmp_path):
    db_path = tmp_path / "state.db"
    _seed_db(db_path)
    client.app.state.state_db_path = str(db_path)
    r = client.post("/api/v1/admin/sql-query", json={"sql": "   "})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "sql_invalid"


def test_limit_truncates(client, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.executemany("INSERT INTO t (id) VALUES (?)", [(1,), (2,), (3,)])
    conn.commit()
    conn.close()
    client.app.state.state_db_path = str(db_path)
    r = client.post(
        "/api/v1/admin/sql-query",
        json={"sql": "SELECT id FROM t ORDER BY id", "limit": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == [[1], [2]]
    assert body["truncated"] is True
    assert body["row_count"] == 2


def test_limit_above_500_is_rejected(client, tmp_path):
    db_path = tmp_path / "state.db"
    _seed_db(db_path)
    client.app.state.state_db_path = str(db_path)
    r = client.post(
        "/api/v1/admin/sql-query",
        json={"sql": "SELECT id FROM t", "limit": 501},
    )
    assert r.status_code == 422


def test_explain_select_is_readonly(client, tmp_path):
    db_path = tmp_path / "state.db"
    _seed_db(db_path)
    client.app.state.state_db_path = str(db_path)
    r = client.post("/api/v1/admin/sql-query", json={"sql": "EXPLAIN SELECT 1"})
    assert r.status_code == 200


def test_normalize_sql_strips_trailing_semicolon():
    assert sql_playground.normalize_sql("SELECT 1;") == "SELECT 1"


def test_normalize_sql_rejects_multiple_statements():
    with pytest.raises(sql_playground.SqlPlaygroundError) as exc:
        sql_playground.normalize_sql("SELECT 1; SELECT 2")
    assert exc.value.code == "sql_invalid"


def test_assert_readonly_sql_rejects_empty():
    with pytest.raises(sql_playground.SqlPlaygroundError) as exc:
        sql_playground.assert_readonly_sql("")
    assert exc.value.code == "sql_invalid"


def test_assert_readonly_sql_accepts_with():
    assert sql_playground.assert_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x") == (
        "WITH x AS (SELECT 1) SELECT * FROM x"
    )


pytestmark = pytest.mark.rb_parity
