"""[if] shared handler errors reach the API [then] bodies are RFC 9457 problem details, [else stop]."""

from __future__ import annotations

import pytest

from tests.webui.app_fixtures import client_fixture  # noqa: F401

pytestmark = pytest.mark.requirement("PLATFORM-13646")

PROBLEM_JSON = "application/problem+json"


@pytest.mark.requirement("PLATFORM-13646")
def test_mapped_not_found_returns_problem_details(client) -> None:
    """[if] a mapped not-found exception reaches the API [then] the body is RFC 9457 404, [else stop]."""
    response = client.get("/api/v1/tracks/nope")
    assert response.status_code == 404
    assert response.headers.get("content-type", "").startswith(PROBLEM_JSON)
    body = response.json()
    assert body["error"] == "not_found"
    assert body["message"]
    assert body["status"] == 404
    assert body["detail"] == body["message"]
    assert body["type"] == "https://opendj.dev/problems/not-found"
    assert body["title"]


@pytest.mark.requirement("PLATFORM-13646")
def test_mapped_conflict_returns_problem_details(client, seed_backend) -> None:
    """[if] a mapped conflict exception reaches the API [then] the body is RFC 9457 409, [else stop]."""
    response = client.patch(
        "/api/v1/tracks/track-001",
        json={"rating": 5},
        headers={"If-Match": '"deadbeef"'},
    )
    assert response.status_code == 409
    assert response.headers.get("content-type", "").startswith(PROBLEM_JSON)
    body = response.json()
    assert body["error"] == "conflict"
    assert body["current"]["stable_id"] == "track-001"
    assert body["status"] == 409
    assert body["detail"] == body["message"]
    assert body["type"] == "https://opendj.dev/problems/conflict"
    assert response.headers.get("ETag")


@pytest.mark.requirement("PLATFORM-13646")
def test_precondition_required_is_problem_details(client) -> None:
    """[if] a problem response is returned from shared handlers [then] Content-Type and RFC fields hold, [else stop]."""
    response = client.patch("/api/v1/tracks/track-001", json={"rating": 5})
    assert response.status_code == 428
    assert response.headers.get("content-type", "").startswith(PROBLEM_JSON)
    body = response.json()
    assert body["error"] == "precondition_required"
    assert body["status"] == 428
    assert body["detail"] == body["message"]
    assert body["type"].startswith("https://opendj.dev/problems/")
    assert body["title"]
