"""LIBM-127: taglists as first-class library navigation (issue #2065).

[if] a mytag is assigned or filtered [then] only tagged tracks appear under that tag, [else stop].
"""

from __future__ import annotations

import pytest

from tests.webui.app_fixtures import client_fixture  # noqa: F401  (pytest fixtures)

from .conftest import current_etag

pytestmark = pytest.mark.requirement("LIBM-127")


def test_assign_mytags_makes_the_track_appear_under_that_tag(client, seed_backend) -> None:
    """[if] POST /api/v1/mytags/assign adds openers to track-002
    [then] GET /api/v1/mytags lists openers and GET /api/v1/tracks?tag=openers
    contains track-002."""
    assign = client.post(
        "/api/v1/mytags/assign",
        json={
            "stable_ids": ["track-002"],
            "expected_etags": {"track-002": current_etag(seed_backend, "track-002")},
            "add": ["openers"],
        },
    )
    assert assign.status_code == 200

    catalog = client.get("/api/v1/mytags")
    assert catalog.status_code == 200
    tags = {row["name"]: row["track_count"] for row in catalog.json()["tags"]}
    assert tags.get("openers", 0) >= 1

    page = client.get("/api/v1/tracks", params={"tag": "openers"})
    assert page.status_code == 200
    ids = {row["stable_id"] for row in page.json()["items"]}
    assert "track-002" in ids
    assert "track-001" not in ids


def test_pin_ad663c171fd0_assign_tag_lists_track(client, seed_backend) -> None:
    """[if] openers tag is assigned to track-002 [then] tag filter lists it, [else stop]."""
    assign = client.post(
        "/api/v1/mytags/assign",
        json={
            "stable_ids": ["track-002"],
            "expected_etags": {"track-002": current_etag(seed_backend, "track-002")},
            "add": ["openers"],
        },
    )
    assert assign.status_code == 200
    page = client.get("/api/v1/tracks", params={"tag": "openers"})
    assert page.status_code == 200
    assert "track-002" in {row["stable_id"] for row in page.json()["items"]}


def test_tracks_tag_filter_excludes_untagged_rows(client) -> None:
    """[if] track-001 has deep-house [then] GET /tracks?tag=deep-house includes
    track-001 and excludes track-002."""
    page = client.get("/api/v1/tracks", params={"tag": "deep-house"})
    assert page.status_code == 200
    ids = {row["stable_id"] for row in page.json()["items"]}
    assert "track-001" in ids
    assert "track-002" not in ids


_TRACK_IDS = [f"track-{i:03d}" for i in range(1, 6)]


def _tag_snapshot(backend) -> dict[str, list[str]]:
    """Capture every seeded track's tag list so a partial rewrite is visible."""
    return {sid: list(backend.get_track(sid).tags or []) for sid in _TRACK_IDS}


def _two_member_scope(client, backend) -> tuple[str, int]:
    """Give deep-house a second member, then return the current rename scope."""
    backend.update_track(
        "track-003",
        {"tags_add": ["deep-house"]},
        expected_etag=current_etag(backend, "track-003"),
    )
    body = client.get("/api/v1/mytags").json()
    counts = {row["name"]: row["track_count"] for row in body["tags"]}
    return body["catalog_revision"], counts["deep-house"]


def _rename(client, revision: str, count: int, new_name: str):
    """POST a deep-house rename against the acknowledged scope without merge consent."""
    return client.post(
        "/api/v1/mytags/rename",
        json={
            "old_name": "deep-house",
            "new_name": new_name,
            "expected_catalog_revision": revision,
            "expected_track_count": count,
        },
    )


@pytest.mark.requirement("CAT-05")
def test_mytag_rename_onto_existing_tag_rewrites_no_member(client, seed_backend) -> None:
    """[if] rename targets an existing tag name without consent [then] 409 and no track tag changes, [else stop]."""
    revision, count = _two_member_scope(client, seed_backend)
    before = _tag_snapshot(seed_backend)
    response = _rename(client, revision, count, "techno")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "mytag_merge_confirmation_required"
    assert _tag_snapshot(seed_backend) == before


@pytest.mark.requirement("CAT-05")
def test_mytag_rename_with_stale_scope_rewrites_no_member(client, seed_backend) -> None:
    """[if] rename carries a stale catalog revision and count [then] 409 and no track tag changes, [else stop]."""
    revision, count = _two_member_scope(client, seed_backend)
    seed_backend.update_track(
        "track-002",
        {"tags_add": ["deep-house"]},
        expected_etag=current_etag(seed_backend, "track-002"),
    )
    before = _tag_snapshot(seed_backend)
    response = _rename(client, revision, count, "house")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_mytag_scope"
    assert response.json()["detail"]["affected_track_count"] == count + 1
    assert _tag_snapshot(seed_backend) == before


@pytest.mark.requirement("CAT-05")
def test_mytag_rename_to_unique_name_rewrites_every_member(client, seed_backend) -> None:
    """[if] rename targets a new unique name with current scope [then] every member and only members move, [else stop]."""
    revision, count = _two_member_scope(client, seed_backend)
    before = _tag_snapshot(seed_backend)
    response = _rename(client, revision, count, "house")
    assert response.status_code == 200
    assert response.json()["tracks_updated"] == 2
    after = _tag_snapshot(seed_backend)
    expected = {sid: [("house" if tag == "deep-house" else tag) for tag in tags] for sid, tags in before.items()}
    assert {sid: sorted(tags) for sid, tags in after.items()} == {sid: sorted(tags) for sid, tags in expected.items()}


def _mytag_scope(client) -> tuple[str, dict[str, int]]:
    """Read the current MyTag catalog revision and per-tag track counts."""
    response = client.get("/api/v1/mytags")
    assert response.status_code == 200
    body = response.json()
    return body["catalog_revision"], {tag["name"]: tag["track_count"] for tag in body["tags"]}


@pytest.mark.requirement("CAT-05")
def test_mytag_delete_of_a_missing_tag_keeps_every_assignment(client) -> None:
    """[if] delete names a tag no track carries [then] it is refused or a no-op and every assignment is retained."""
    revision, counts = _mytag_scope(client)
    assert "no-such-tag" not in counts

    refused = client.post(
        "/api/v1/mytags/delete",
        json={"name": "no-such-tag", "expected_catalog_revision": revision, "expected_track_count": 1},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "stale_mytag_scope"
    assert refused.json()["detail"]["affected_track_count"] == 0
    assert _mytag_scope(client) == (revision, counts)

    noop = client.post(
        "/api/v1/mytags/delete",
        json={"name": "no-such-tag", "expected_catalog_revision": revision, "expected_track_count": 0},
    )
    assert noop.status_code == 200
    assert noop.json()["tracks_updated"] == 0
    assert _mytag_scope(client) == (revision, counts)


@pytest.mark.requirement("CAT-05")
def test_mytag_delete_with_stale_expected_assignments_keeps_every_assignment(client, seed_backend) -> None:
    """[if] delete carries a stale revision or member count [then] 409 and no tag on any track is removed."""
    revision, counts = _mytag_scope(client)
    seed_backend.update_track(
        "track-002",
        {"tags_add": ["deep-house"]},
        expected_etag=current_etag(seed_backend, "track-002"),
    )
    _, after_counts = _mytag_scope(client)

    for expected_count in (counts["deep-house"], after_counts["deep-house"]):
        response = client.post(
            "/api/v1/mytags/delete",
            json={"name": "deep-house", "expected_catalog_revision": revision, "expected_track_count": expected_count},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "stale_mytag_scope"
        assert _mytag_scope(client)[1] == after_counts
    assert "deep-house" in seed_backend.get_track("track-001").tags
    assert "deep-house" in seed_backend.get_track("track-002").tags


@pytest.mark.requirement("CAT-05")
def test_mytag_delete_with_current_scope_clears_only_the_named_tag(client) -> None:
    """[if] delete names an existing tag with the current scope [then] only that tag is cleared, others stay."""
    revision, counts = _mytag_scope(client)
    others = {name: count for name, count in counts.items() if name != "deep-house"}
    assert others

    response = client.post(
        "/api/v1/mytags/delete",
        json={
            "name": "deep-house",
            "expected_catalog_revision": revision,
            "expected_track_count": counts["deep-house"],
        },
    )
    assert response.status_code == 200
    assert response.json()["tracks_updated"] == counts["deep-house"]

    page = client.get("/api/v1/tracks", params={"tag": "deep-house"})
    assert page.status_code == 200
    assert page.json()["items"] == []
    assert _mytag_scope(client)[1] == others
