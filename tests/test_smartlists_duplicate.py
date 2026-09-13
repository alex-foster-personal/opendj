"""LIBMX-04: POST /api/v1/smartlists/{id}/duplicate regression tests (issue #2445)."""

from __future__ import annotations

import pytest

from tests.test_smartlists_route import (
    _BPM_RULE,
    _create_smartlist,
    _make_client,
)

pytest_plugins = ["tests.test_smartlists_route"]


@pytest.mark.requirement("LIBMX-04")
def test_duplicate_smartlist_default_name(client, state_db_path):
    """[if] POST duplicate [then] 201 with same rule and '(copy)' name."""
    sid = _create_smartlist(state_db_path, "source", _BPM_RULE)
    r = client.post(f"/api/v1/smartlists/{sid}/duplicate")
    assert r.status_code == 201
    body = r.json()
    assert body["id"] != sid
    assert body["name"] == "source (copy)"
    assert body["rule"] == _BPM_RULE

    listed = client.get("/api/v1/smartlists")
    ids = {row["id"] for row in listed.json()}
    assert ids == {sid, body["id"]}


@pytest.mark.requirement("LIBMX-04")
def test_duplicate_smartlist_custom_name(client, state_db_path):
    """[if] POST duplicate with name override [then] override is honored."""
    sid = _create_smartlist(state_db_path, "src", _BPM_RULE)
    r = client.post(
        f"/api/v1/smartlists/{sid}/duplicate",
        json={"name": "custom copy"},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "custom copy"


@pytest.mark.requirement("LIBMX-04")
def test_duplicate_unknown_id_404(client):
    """[if] unknown source id [then] 404 SMARTLIST_NOT_FOUND."""
    r = client.post("/api/v1/smartlists/nope/duplicate")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SMARTLIST_NOT_FOUND"


@pytest.mark.requirement("LIBMX-04")
def test_duplicate_tombstoned_source_404(client, state_db_path):
    """[if] source is tombstoned [then] 404 SMARTLIST_NOT_FOUND."""
    sid = _create_smartlist(state_db_path, "gone", _BPM_RULE)
    assert client.delete(f"/api/v1/smartlists/{sid}").status_code == 204
    r = client.post(f"/api/v1/smartlists/{sid}/duplicate")
    assert r.status_code == 404


@pytest.mark.requirement("LIBMX-04")
def test_duplicate_peer_lock_503(client, state_db_path):
    """[if] peer lock held [then] 503 and no extra row."""
    sid = _create_smartlist(state_db_path, "locked-src", _BPM_RULE)
    before = client.get("/api/v1/smartlists").json()
    with _make_client(
        state_db_path,
        lock_status_fn=lambda: {"holder": "other-host"},
    ) as locked_client:
        r = locked_client.post(f"/api/v1/smartlists/{sid}/duplicate")
    assert r.status_code == 503
    after = client.get("/api/v1/smartlists").json()
    assert len(after) == len(before)


@pytest.mark.requirement("LIBMX-04")
def test_duplicate_openapi_documents_route(client):
    """[if] OpenAPI is dumped [then] duplicate route returns 201."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/api/v1/smartlists/{smartlist_id}/duplicate"]["post"]
    assert "201" in op["responses"]
