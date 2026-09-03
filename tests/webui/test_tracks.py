"""Tracks endpoint tests (CAT-05)."""
from __future__ import annotations

import pytest

from .conftest import current_etag


@pytest.mark.requirement("CAT-05")
def test_list_tracks_happy_path(client):
    r = client.get("/api/v1/tracks")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 5
    assert body["next_cursor"] is None


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
    r2 = client.get("/api/v1/tracks",
                    params={"limit": 2, "cursor": body["next_cursor"]})
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
        "/api/v1/tracks/track-001", json={"rating": 5},
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
        "/api/v1/tracks/track-001", json={"rating": 2},
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
        "/api/v1/tracks/track-001", json={"rating": 99},
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
        "/api/v1/tracks/track-002", json={"notes": "my first mix track"},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200
    assert r.json()["notes"] == "my first mix track"


@pytest.mark.requirement("PREF-01")
def test_get_track_tempo_pref_unset_is_explicit_null(client):
    """A track nobody has edited yet reports null, never a fabricated range."""
    r = client.get("/api/v1/tracks/track-003")
    assert r.status_code == 200
    assert r.json()["tempo_pref"] is None


@pytest.mark.requirement("PREF-01")
def test_patch_track_tempo_pref_round_trips(client, seed_backend):
    etag = current_etag(seed_backend, "track-003")
    r = client.patch(
        "/api/v1/tracks/track-003",
        json={"tempo_pref": {"regular": 140.0, "min": 138.0, "max": 142.0}},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200
    assert r.json()["tempo_pref"] == {"regular": 140.0, "min": 138.0, "max": 142.0}
    # Independent GET confirms real persistence, not just an echoed body.
    reread = client.get("/api/v1/tracks/track-003")
    assert reread.json()["tempo_pref"] == {"regular": 140.0, "min": 138.0, "max": 142.0}


@pytest.mark.requirement("PREF-01")
def test_patch_track_tempo_pref_min_gte_max_422(client, seed_backend):
    etag = current_etag(seed_backend, "track-003")
    r = client.patch(
        "/api/v1/tracks/track-003",
        json={"tempo_pref": {"regular": 140.0, "min": 145.0, "max": 145.0}},
        headers={"If-Match": etag},
    )
    assert r.status_code == 422
    # Nothing persisted from the rejected patch.
    assert client.get("/api/v1/tracks/track-003").json()["tempo_pref"] is None


@pytest.mark.requirement("PREF-01")
def test_patch_track_tempo_pref_clamps_regular_into_new_range(client, seed_backend):
    """Trap case: a regular value now outside a newly-set range is clamped
    into it rather than left out of bounds."""
    etag = current_etag(seed_backend, "track-004")
    r = client.patch(
        "/api/v1/tracks/track-004",
        json={"tempo_pref": {"regular": 200.0, "min": 138.0, "max": 142.0}},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200
    assert r.json()["tempo_pref"] == {"regular": 142.0, "min": 138.0, "max": 142.0}


@pytest.mark.requirement("PREF-01")
def test_patch_track_tempo_pref_clear_to_null(client, seed_backend):
    etag = current_etag(seed_backend, "track-005")
    set_r = client.patch(
        "/api/v1/tracks/track-005",
        json={"tempo_pref": {"regular": 95.0, "min": None, "max": None}},
        headers={"If-Match": etag},
    )
    assert set_r.json()["tempo_pref"] == {"regular": 95.0, "min": None, "max": None}
    clear_r = client.patch(
        "/api/v1/tracks/track-005", json={"tempo_pref": None},
        headers={"If-Match": set_r.headers["ETag"]},
    )
    assert clear_r.status_code == 200
    assert clear_r.json()["tempo_pref"] is None
