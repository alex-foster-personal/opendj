"""LIBMX-01: PUT /api/v1/smartlists/{id} rename regression tests."""

from __future__ import annotations

import pytest

from tests.test_smartlists_route import (
    _BPM_RULE,
    _create_smartlist,
    _make_client,
)

pytest_plugins = ["tests.test_smartlists_route"]


@pytest.mark.requirement("LIBMX-01")
def test_rename_smartlist_with_if_match(client, state_db_path):
    """[if] PUT includes name + If-Match [then] GET round-trips the new name, [else stop]."""
    sid = _create_smartlist(state_db_path, "before", _BPM_RULE)
    detail = client.get(f"/api/v1/smartlists/{sid}")
    assert detail.status_code == 200
    etag = detail.headers["etag"]
    r = client.put(
        f"/api/v1/smartlists/{sid}",
        json={"rule": _BPM_RULE, "name": "after"},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "after"
    assert r.headers["etag"] != etag

    listed = client.get("/api/v1/smartlists")
    assert listed.json()[0]["name"] == "after"
    again = client.get(f"/api/v1/smartlists/{sid}")
    assert again.json()["name"] == "after"


@pytest.mark.requirement("LIBMX-01")
def test_rename_missing_if_match_428(client, state_db_path):
    """[if] PUT omits If-Match [then] 428 and name unchanged, [else stop]."""
    sid = _create_smartlist(state_db_path, "locked-name", _BPM_RULE)
    r = client.put(
        f"/api/v1/smartlists/{sid}",
        json={"rule": _BPM_RULE, "name": "nope"},
    )
    assert r.status_code == 428
    detail = client.get(f"/api/v1/smartlists/{sid}")
    assert detail.json()["name"] == "locked-name"


@pytest.mark.requirement("LIBMX-01")
def test_rename_stale_if_match_409(client, state_db_path):
    """[if] If-Match is stale [then] 409 conflict and name unchanged, [else stop]."""
    sid = _create_smartlist(state_db_path, "stale", _BPM_RULE)
    r = client.put(
        f"/api/v1/smartlists/{sid}",
        json={"rule": _BPM_RULE, "name": "new"},
        headers={"If-Match": '"deadbeef"'},
    )
    assert r.status_code == 409
    detail = client.get(f"/api/v1/smartlists/{sid}")
    assert detail.json()["name"] == "stale"


@pytest.mark.requirement("LIBMX-01")
def test_rename_duplicate_live_name_409(client, state_db_path):
    """[if] rename collides with a live name [then] 409 SMARTLIST_NAME_CONFLICT, [else stop]."""
    _create_smartlist(state_db_path, "taken", _BPM_RULE)
    sid = _create_smartlist(state_db_path, "mine", _BPM_RULE)
    etag = client.get(f"/api/v1/smartlists/{sid}").headers["etag"]
    r = client.put(
        f"/api/v1/smartlists/{sid}",
        json={"rule": _BPM_RULE, "name": "taken"},
        headers={"If-Match": etag},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SMARTLIST_NAME_CONFLICT"


@pytest.mark.requirement("LIBMX-01")
def test_rename_peer_lock_503(client, state_db_path):
    """[if] peer lock held [then] PUT 503 and name unchanged, [else stop]."""
    sid = _create_smartlist(state_db_path, "peer", _BPM_RULE)
    etag = client.get(f"/api/v1/smartlists/{sid}").headers["etag"]
    with _make_client(
        state_db_path,
        lock_status_fn=lambda: {"holder": "other-host"},
    ) as locked_client:
        r = locked_client.put(
            f"/api/v1/smartlists/{sid}",
            json={"rule": _BPM_RULE, "name": "blocked"},
            headers={"If-Match": etag},
        )
    assert r.status_code == 503
    detail = client.get(f"/api/v1/smartlists/{sid}")
    assert detail.json()["name"] == "peer"


@pytest.mark.requirement("LIBMX-01")
def test_rename_openapi_includes_name(client):
    """[if] OpenAPI is dumped [then] SmartlistUpdateIn has name, [else stop]."""
    spec = client.get("/openapi.json").json()
    props = spec["components"]["schemas"]["SmartlistUpdateIn"]["properties"]
    assert "name" in props
