"""Regression coverage for the edit-suite HTTP contracts."""

from __future__ import annotations

import pytest

from .conftest import current_etag


@pytest.mark.requirement("CAT-05")
def test_bulk_edit_stale_member_leaves_the_entire_selection_unchanged(
    client, seed_backend
) -> None:
    before = [
        seed_backend.get_track(stable_id).rating
        for stable_id in ("track-001", "track-002")
    ]
    response = client.patch(
        "/api/v1/bulk-edit",
        json={
            "stable_ids": ["track-001", "track-002"],
            "expected_etags": {
                "track-001": '"stale"',
                "track-002": current_etag(seed_backend, "track-002"),
            },
            "rating": 1,
        },
    )
    assert response.status_code == 409
    assert [
        seed_backend.get_track(stable_id).rating
        for stable_id in ("track-001", "track-002")
    ] == before


@pytest.mark.requirement("CAT-05")
def test_find_replace_rejects_nested_quantifiers_without_writing(
    client, seed_backend
) -> None:
    seed_backend.get_track("track-001").notes = "a" * 100
    response = client.post(
        "/api/v1/find-replace/preview",
        json={"stable_ids": ["track-001"], "find": "(a+)+$", "mode": "regex"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unsafe_regex"
    assert seed_backend.get_track("track-001").notes == "a" * 100


@pytest.mark.requirement("CAT-05")
def test_find_replace_rejects_ambiguous_repeated_alternation_without_writing(
    client, seed_backend
) -> None:
    seed_backend.get_track("track-001").notes = "a" * 100 + "b"
    response = client.post(
        "/api/v1/find-replace/preview",
        json={"stable_ids": ["track-001"], "find": "^(a|aa)+$", "mode": "regex"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unsafe_regex"
    assert (
        response.json()["detail"]["message"] == "regex exceeded the safety time limit"
    )
    assert seed_backend.get_track("track-001").notes == "a" * 100 + "b"


@pytest.mark.requirement("CAT-05")
def test_find_replace_allows_safe_quantified_alternation_without_writing(
    client, seed_backend
) -> None:
    seed_backend.get_track("track-001").notes = "foobarfoo"
    response = client.post(
        "/api/v1/find-replace/preview",
        json={
            "stable_ids": ["track-001"],
            "find": "(foo|bar)+",
            "replace": "matched",
            "mode": "regex",
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["new_value"] == "matched"
    assert seed_backend.get_track("track-001").notes == "foobarfoo"


@pytest.mark.requirement("CAT-05")
def test_find_replace_rejects_duplicate_stable_ids_without_writing(
    client, seed_backend
) -> None:
    seed_backend.get_track("track-001").notes = "before"
    response = client.post(
        "/api/v1/find-replace/preview",
        json={
            "stable_ids": ["track-001", "track-001"],
            "find": "before",
            "replace": "after",
        },
    )
    assert response.status_code == 422
    assert seed_backend.get_track("track-001").notes == "before"


@pytest.mark.requirement("CAT-05")
def test_find_replace_applies_valid_regex_without_writing(client, seed_backend) -> None:
    seed_backend.get_track("track-001").notes = "Midnight Drive"
    response = client.post(
        "/api/v1/find-replace/preview",
        json={
            "stable_ids": ["track-001"],
            "find": "^midnight",
            "replace": "Late Night",
            "mode": "regex",
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["new_value"] == "Late Night Drive"
    assert seed_backend.get_track("track-001").notes == "Midnight Drive"


@pytest.mark.requirement("CAT-05")
def test_find_replace_preserves_literal_metacharacters_without_writing(
    client, seed_backend
) -> None:
    seed_backend.get_track("track-001").notes = "A.B"
    response = client.post(
        "/api/v1/find-replace/preview",
        json={
            "stable_ids": ["track-001"],
            "find": "A.B",
            "replace": "C",
            "mode": "literal",
            "case_sensitive": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["new_value"] == "C"
    assert seed_backend.get_track("track-001").notes == "A.B"


@pytest.mark.requirement("CAT-05")
def test_find_replace_converts_a_real_regex_timeout_to_an_explicit_client_error(
    client, seed_backend
) -> None:
    seed_backend.get_track("track-001").notes = "a" * 100 + "b"
    response = client.post(
        "/api/v1/find-replace/preview",
        json={"stable_ids": ["track-001"], "find": "^((a|aa))+$", "mode": "regex"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "unsafe_regex",
        "message": "regex exceeded the safety time limit",
    }
    assert seed_backend.get_track("track-001").notes == "a" * 100 + "b"


@pytest.mark.requirement("CAT-05")
def test_mytag_assign_uses_one_batch_cas(client, seed_backend) -> None:
    response = client.post(
        "/api/v1/mytags/assign",
        json={
            "stable_ids": ["track-001", "track-002"],
            "expected_etags": {
                "track-001": current_etag(seed_backend, "track-001"),
                "track-002": current_etag(seed_backend, "track-002"),
            },
            "add": ["opener"],
        },
    )
    assert response.status_code == 200
    assert all(
        "opener" in seed_backend.get_track(stable_id).tags
        for stable_id in ("track-001", "track-002")
    )
