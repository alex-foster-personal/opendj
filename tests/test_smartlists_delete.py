"""LIBM-83: DELETE /api/v1/smartlists/{id} regression tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from tests.test_smartlists_route import (
    _BPM_RULE,
    _create_smartlist,
    _make_client,
)

pytest_plugins = ["tests.test_smartlists_route"]


@pytest.mark.requirement("LIBM-83")
def test_delete_smartlist_create_delete_list_absent(client, state_db_path):
    """[if] DELETE on an existing smartlist [then] 204 and absent from list/GET."""
    sid = _create_smartlist(state_db_path, "deletable", _BPM_RULE)
    r = client.delete(f"/api/v1/smartlists/{sid}")
    assert r.status_code == 204
    assert r.content == b""

    listed = client.get("/api/v1/smartlists")
    assert listed.status_code == 200
    assert sid not in {row["id"] for row in listed.json()}

    detail = client.get(f"/api/v1/smartlists/{sid}")
    assert detail.status_code == 404
    assert detail.json()["detail"]["code"] == "SMARTLIST_NOT_FOUND"

    conn = sqlite3.connect(str(state_db_path))
    try:
        row = conn.execute(
            "SELECT name, deleted_at FROM smartlists WHERE id=?", (sid,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[1] is not None
    assert "__deleted__" in row[0]
    assert sid in row[0]

    recreated = client.post(
        "/api/v1/smartlists",
        json={"name": "deletable", "rule": _BPM_RULE},
    )
    assert recreated.status_code == 201, recreated.text


@pytest.mark.requirement("LIBM-83")
def test_delete_smartlist_unknown_id_404(client):
    """[if] DELETE on unknown id [then] 404 SMARTLIST_NOT_FOUND."""
    r = client.delete("/api/v1/smartlists/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SMARTLIST_NOT_FOUND"


@pytest.mark.requirement("LIBM-83")
def test_delete_smartlist_peer_cloud_lock_503_leaves_row(state_db_path):
    """[if] peer lock held [then] DELETE 503 and row still GET-able."""
    sid = _create_smartlist(state_db_path, "locked", _BPM_RULE)
    with _make_client(
        state_db_path,
        lock_status_fn=lambda: {"holder": "other-host"},
    ) as locked_client:
        r = locked_client.delete(f"/api/v1/smartlists/{sid}")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "locked_by_peer"
    with _make_client(state_db_path) as read_client:
        detail = read_client.get(f"/api/v1/smartlists/{sid}")
    assert detail.status_code == 200


@pytest.mark.requirement("LIBM-83")
def test_delete_smartlist_openapi_documents_delete(client):
    """[if] OpenAPI is dumped [then] DELETE route returns 204."""
    spec = client.get("/openapi.json").json()
    delete_op = spec["paths"]["/api/v1/smartlists/{smartlist_id}"]["delete"]
    assert "204" in delete_op["responses"]


@pytest.mark.requirement("LIBM-83")
def test_delete_smartlist_missing_table_404(tmp_path: Path):
    """[if] smartlists table absent [then] DELETE 404 not 500."""
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path, apply_schema=True)
    conn.close()
    with _make_client(path) as c:
        r = c.delete("/api/v1/smartlists/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SMARTLIST_NOT_FOUND"
