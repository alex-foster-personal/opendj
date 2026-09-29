"""LIBM-127: taglists as first-class library navigation (issue #2065).

[if] a mytag is assigned or filtered [then] only tagged tracks appear under that tag, [else stop].
"""

from __future__ import annotations

import pytest

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
