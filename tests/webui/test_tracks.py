"""Tracks endpoint tests (CAT-05)."""

from __future__ import annotations

import pytest

from apps.webui.server.backend import Provenance, Track

from .conftest import current_etag


@pytest.mark.requirement("CAT-05")
def test_list_tracks_happy_path(client):
    r = client.get("/api/v1/tracks")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 5
    assert body["next_cursor"] is None


@pytest.mark.requirement("CAT-05")
def test_list_tracks_includes_artwork_facts_without_rb_meta_hydration(client):
    """The table can decide whether to render art from its listing row alone."""
    response = client.get("/api/v1/tracks")

    assert response.status_code == 200
    for item in response.json()["items"]:
        assert isinstance(item["artwork_available"], bool) or item["artwork_available"] is None
        assert item["artwork_status"] in {
            "ok",
            "no_image_path",
            "unresolved",
            "file_missing",
        }


@pytest.mark.requirement("META-01")
def test_list_tracks_exposes_only_readable_mik_energy_with_provenance(client, seed_backend):
    """The narrow Energy column never receives a guessed or computed value."""
    stamp = "2026-09-05T12:00:00Z"
    seed_backend.seed_track(
        Track(
            stable_id="energy-mik",
            title="MIK",
            created_at=stamp,
            updated_at=stamp,
            provenance={"energy": Provenance(7, "mik", 1.0, stamp, "ok")},
        )
    )
    seed_backend.seed_track(
        Track(
            stable_id="energy-inferred",
            title="Computed",
            created_at=stamp,
            updated_at=stamp,
            provenance={"energy": Provenance(8, "inferred", 1.0, stamp, "ok")},
        )
    )
    seed_backend.seed_track(
        Track(
            stable_id="energy-ten",
            title="Ten",
            created_at=stamp,
            updated_at=stamp,
            provenance={"energy": Provenance(10, "mik", 1.0, stamp, "ok")},
        )
    )

    response = client.get("/api/v1/tracks")
    assert response.status_code == 200, response.text
    rows = {item["stable_id"]: item for item in response.json()["items"]}

    assert rows["energy-mik"]["energy"] == 7
    assert rows["energy-mik"]["energy_source"] == "mik"
    assert rows["energy-inferred"]["energy"] is None
    assert rows["energy-inferred"]["energy_source"] is None
    assert rows["energy-inferred"]["energy_reason"] == "source is inferred, not Mixed In Key"
    assert rows["energy-ten"]["energy"] is None
    assert rows["energy-ten"]["energy_reason"] == (
        "Mixed In Key value 10 is outside the 1-9 display scale"
    )


@pytest.mark.requirement("CAT-05")
def test_list_tracks_filter_q(client):
    r = client.get("/api/v1/tracks", params={"q": "midnight"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Midnight Drive"


@pytest.mark.requirement("CAT-05")
def test_list_tracks_filter_bpm_range(client):
    r = client.get("/api/v1/tracks", params={"bpm_min": 120, "bpm_max": 130})
    items = r.json()["items"]
    assert {t["stable_id"] for t in items} == {"track-001", "track-002"}


@pytest.mark.requirement("CAT-05")
def test_list_tracks_filter_rating_min(client):
    r = client.get("/api/v1/tracks", params={"rating_min": 4})
    items = r.json()["items"]
    assert {t["stable_id"] for t in items} == {"track-001", "track-003", "track-005"}


@pytest.mark.requirement("CAT-05")
def test_list_tracks_pagination(client):
    r = client.get("/api/v1/tracks", params={"limit": 2})
    body = r.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None
    r2 = client.get("/api/v1/tracks", params={"limit": 2, "cursor": body["next_cursor"]})
    body2 = r2.json()
    assert len(body2["items"]) == 2
    assert body2["items"][0]["stable_id"] != body["items"][0]["stable_id"]


@pytest.mark.requirement("CAT-05")
def test_get_track_returns_provenance(client):
    r = client.get("/api/v1/tracks/track-001")
    assert r.status_code == 200
    assert r.headers.get("ETag")
    body = r.json()
    assert body["stable_id"] == "track-001"
    assert "rating" in body["provenance"]
    assert body["provenance"]["rating"]["source"] == "rekordbox"


@pytest.mark.requirement("CAT-05")
def test_get_track_404(client):
    r = client.get("/api/v1/tracks/nope")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


@pytest.mark.requirement("CAT-05")
def test_patch_track_without_if_match_returns_428(client):
    r = client.patch("/api/v1/tracks/track-001", json={"rating": 5})
    assert r.status_code == 428
    assert r.json()["error"] == "precondition_required"


@pytest.mark.requirement("CAT-05")
def test_patch_track_with_stale_if_match_returns_409(client, seed_backend):
    r = client.patch(
        "/api/v1/tracks/track-001",
        json={"rating": 5},
        headers={"If-Match": '"deadbeef"'},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "conflict"
    assert body["current"]["stable_id"] == "track-001"
    assert r.headers.get("ETag")


@pytest.mark.requirement("CAT-05")
def test_patch_track_with_good_if_match_succeeds(client, seed_backend):
    etag = current_etag(seed_backend, "track-001")
    r = client.patch(
        "/api/v1/tracks/track-001",
        json={"rating": 2},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rating"] == 2
    new_etag = r.headers.get("ETag")
    assert new_etag and new_etag != etag
    assert body["provenance"]["rating"]["source"] == "webui"


@pytest.mark.requirement("CAT-05")
def test_patch_track_rating_out_of_range_422(client, seed_backend):
    etag = current_etag(seed_backend, "track-001")
    r = client.patch(
        "/api/v1/tracks/track-001",
        json={"rating": 99},
        headers={"If-Match": etag},
    )
    assert r.status_code == 422


@pytest.mark.requirement("CAT-05")
def test_patch_track_tags_add_remove(client, seed_backend):
    etag = current_etag(seed_backend, "track-001")
    r = client.patch(
        "/api/v1/tracks/track-001",
        json={"tags_add": ["new-tag"], "tags_remove": ["deep-house"]},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200
    tags = r.json()["tags"]
    assert "new-tag" in tags
    assert "deep-house" not in tags


@pytest.mark.requirement("CAT-05")
def test_patch_track_notes(client, seed_backend):
    etag = current_etag(seed_backend, "track-002")
    r = client.patch(
        "/api/v1/tracks/track-002",
        json={"notes": "my first mix track"},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200
    assert r.json()["notes"] == "my first mix track"


pytestmark = pytest.mark.rb_parity
