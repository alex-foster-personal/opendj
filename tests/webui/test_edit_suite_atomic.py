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


def _mytag_scope(client) -> tuple[str, dict[str, int]]:
    response = client.get("/api/v1/mytags")
    assert response.status_code == 200
    body = response.json()
    return body["catalog_revision"], {tag["name"]: tag["track_count"] for tag in body["tags"]}


@pytest.mark.requirement("CAT-05")
def test_mytag_delete_requires_current_catalog_revision_and_count(client, seed_backend) -> None:
    revision, counts = _mytag_scope(client)
    response = client.post(
        "/api/v1/mytags/delete",
        json={
            "name": "deep-house",
            "expected_catalog_revision": revision,
            "expected_track_count": counts["deep-house"] + 1,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_mytag_scope"
    assert seed_backend.get_track("track-001").tags == ["deep-house"]


@pytest.mark.requirement("CAT-05")
def test_mytag_delete_rejects_a_stale_catalog_revision_when_count_still_matches(client, seed_backend) -> None:
    revision, counts = _mytag_scope(client)
    seed_backend.update_track(
        "track-002",
        {"tags_add": ["late"]},
        expected_etag=current_etag(seed_backend, "track-002"),
    )
    response = client.post(
        "/api/v1/mytags/delete",
        json={
            "name": "deep-house",
            "expected_catalog_revision": revision,
            "expected_track_count": counts["deep-house"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_mytag_scope"
    assert response.json()["detail"]["affected_track_count"] == 1
    assert seed_backend.get_track("track-001").tags == ["deep-house"]


@pytest.mark.requirement("CAT-05")
def test_mytag_rename_collision_requires_explicit_merge_consent(client, seed_backend) -> None:
    revision, counts = _mytag_scope(client)
    response = client.post(
        "/api/v1/mytags/rename",
        json={
            "old_name": "deep-house",
            "new_name": "techno",
            "expected_catalog_revision": revision,
            "expected_track_count": counts["deep-house"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "mytag_merge_confirmation_required"
    assert seed_backend.get_track("track-001").tags == ["deep-house"]


@pytest.mark.requirement("CAT-05")
def test_mytag_rename_collision_merges_only_with_explicit_consent(client, seed_backend) -> None:
    revision, counts = _mytag_scope(client)
    response = client.post(
        "/api/v1/mytags/rename",
        json={
            "old_name": "deep-house",
            "new_name": "techno",
            "expected_catalog_revision": revision,
            "expected_track_count": counts["deep-house"],
            "confirm_merge": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["tracks_updated"] == 1
    assert seed_backend.get_track("track-001").tags == ["techno"]


@pytest.mark.requirement("CAT-05")
def test_mytag_delete_with_current_scope_removes_all_members(client, seed_backend) -> None:
    revision, counts = _mytag_scope(client)
    response = client.post(
        "/api/v1/mytags/delete",
        json={
            "name": "ambient",
            "expected_catalog_revision": revision,
            "expected_track_count": counts["ambient"],
        },
    )
    assert response.status_code == 200
    assert response.json()["tracks_updated"] == 1
    assert seed_backend.get_track("track-003").tags == ["downtempo"]


@pytest.mark.requirement("CAT-05")
def test_mytag_transaction_rejects_member_added_after_scope_observation(client, seed_backend) -> None:
    revision, counts = _mytag_scope(client)
    seed_backend.update_track(
        "track-002",
        {"tags_add": ["deep-house"]},
        expected_etag=current_etag(seed_backend, "track-002"),
    )
    response = client.post(
        "/api/v1/mytags/delete",
        json={
            "name": "deep-house",
            "expected_catalog_revision": revision,
            "expected_track_count": counts["deep-house"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": "stale_mytag_scope",
        "catalog_revision": _mytag_scope(client)[0],
        "affected_track_count": 2,
    }
    assert "deep-house" in seed_backend.get_track("track-001").tags
    assert "deep-house" in seed_backend.get_track("track-002").tags


@pytest.mark.requirement("CAT-05")
def test_mytag_openapi_documents_stale_scope_and_merge_confirmation_conflicts(client) -> None:
    schema = client.get("/openapi.json").json()
    delete_conflict = schema["paths"]["/api/v1/mytags/delete"]["post"]["responses"]["409"]
    rename_conflict = schema["paths"]["/api/v1/mytags/rename"]["post"]["responses"]["409"]

    assert delete_conflict["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/MyTagScopeConflictOut",
    }
    assert rename_conflict["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/MyTagRenameConflictOut",
    }
    detail_schema = schema["components"]["schemas"]["MyTagRenameConflictOut"]["properties"]["detail"]
    assert detail_schema["anyOf"] == [
        {"$ref": "#/components/schemas/MyTagScopeConflictDetail"},
        {"$ref": "#/components/schemas/MyTagMergeConflictDetail"},
    ]

@pytest.mark.requirement("LIBM-62")
def test_bulk_edit_sets_genre_on_every_selected_row(client, seed_backend) -> None:
    before_notes = [
        seed_backend.get_track(stable_id).notes
        for stable_id in ("track-001", "track-002")
    ]
    before_rating = [
        seed_backend.get_track(stable_id).rating
        for stable_id in ("track-001", "track-002")
    ]
    response = client.patch(
        "/api/v1/bulk-edit",
        json={
            "stable_ids": ["track-001", "track-002"],
            "expected_etags": {
                "track-001": current_etag(seed_backend, "track-001"),
                "track-002": current_etag(seed_backend, "track-002"),
            },
            "genre": "Deep House",
        },
    )
    assert response.status_code == 200
    assert response.json()["applied_count"] == 2
    assert seed_backend.get_track("track-001").genre == "Deep House"
    assert seed_backend.get_track("track-002").genre == "Deep House"
    assert [
        seed_backend.get_track(stable_id).notes for stable_id in ("track-001", "track-002")
    ] == before_notes
    assert [
        seed_backend.get_track(stable_id).rating for stable_id in ("track-001", "track-002")
    ] == before_rating


@pytest.mark.requirement("LIBM-62")
def test_bulk_edit_stale_member_leaves_genre_unchanged(client, seed_backend) -> None:
    seed_backend.get_track("track-001").genre = "House"
    seed_backend.get_track("track-002").genre = "House"
    before = [
        seed_backend.get_track(stable_id).genre
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
            "genre": "Deep House",
        },
    )
    assert response.status_code == 409
    assert [
        seed_backend.get_track(stable_id).genre
        for stable_id in ("track-001", "track-002")
    ] == before


@pytest.mark.requirement("LIBM-66")
def test_find_replace_preview_on_genre(client, seed_backend) -> None:
    seed_backend.get_track("track-001").genre = "House"
    seed_backend.get_track("track-002").genre = "Techno"
    response = client.post(
        "/api/v1/find-replace/preview",
        json={
            "field": "genre",
            "stable_ids": ["track-001", "track-002"],
            "find": "House",
            "replace": "Deep House",
        },
    )
    assert response.status_code == 200
    body = response.json()
    by_id = {row["stable_id"]: row for row in body["results"]}
    assert by_id["track-001"]["would_change"] is True
    assert by_id["track-001"]["current_value"] == "House"
    assert by_id["track-001"]["new_value"] == "Deep House"
    assert by_id["track-002"]["would_change"] is False
    assert seed_backend.get_track("track-001").genre == "House"
    assert seed_backend.get_track("track-002").genre == "Techno"


@pytest.mark.requirement("LIBM-66")
def test_find_replace_apply_on_genre(client, seed_backend) -> None:
    seed_backend.get_track("track-001").genre = "House"
    seed_backend.get_track("track-001").notes = "keep me"
    seed_backend.get_track("track-002").genre = "Techno"
    response = client.post(
        "/api/v1/find-replace/apply",
        json={
            "field": "genre",
            "stable_ids": ["track-001", "track-002"],
            "find": "House",
            "replace": "Deep House",
            "expected_etags": {
                "track-001": current_etag(seed_backend, "track-001"),
                "track-002": current_etag(seed_backend, "track-002"),
            },
        },
    )
    assert response.status_code == 200
    assert seed_backend.get_track("track-001").genre == "Deep House"
    assert seed_backend.get_track("track-002").genre == "Techno"
    assert seed_backend.get_track("track-001").notes == "keep me"
    results = {row["stable_id"]: row for row in response.json()["results"]}
    assert results["track-001"]["new_value"] == "Deep House"


@pytest.mark.requirement("LIBM-66")
def test_find_replace_preview_on_comments(client, seed_backend) -> None:
    seed_backend.get_track("track-001").comments = "warm opener"
    response = client.post(
        "/api/v1/find-replace/preview",
        json={
            "field": "comments",
            "stable_ids": ["track-001"],
            "find": "warm",
            "replace": "hot",
        },
    )
    assert response.status_code == 200
    row = response.json()["results"][0]
    assert row["current_value"] == "warm opener"
    assert row["new_value"] == "hot opener"
    assert row["would_change"] is True
    assert seed_backend.get_track("track-001").comments == "warm opener"


@pytest.mark.requirement("LIBM-66")
def test_find_replace_rejects_unknown_field(client, seed_backend) -> None:
    seed_backend.get_track("track-001").notes = "before"
    response = client.post(
        "/api/v1/find-replace/preview",
        json={
            "field": "title",
            "stable_ids": ["track-001"],
            "find": "before",
            "replace": "after",
        },
    )
    assert response.status_code == 422
    assert seed_backend.get_track("track-001").notes == "before"


@pytest.mark.requirement("LIBM-66")
def test_find_replace_openapi_documents_genre_and_comments_fields(client) -> None:
    schema = client.get("/openapi.json").json()
    field_enum = schema["components"]["schemas"]["FindReplacePreviewIn"]["properties"]["field"][
        "enum"
    ]
    assert field_enum == ["notes", "genre", "comments"]
    bulk_props = schema["components"]["schemas"]["BulkEditIn"]["properties"]
    assert "genre" in bulk_props
    assert "comments" in bulk_props


pytestmark = pytest.mark.rb_parity
