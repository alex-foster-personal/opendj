"""Store cover suggestions over HTTP and the CLI (LIBM-188, LIBM-189).

[if] a pending store cover is listed or decided [then] consent and cache rules hold, [else stop].

A real ``create_app`` engine and a real on-disk artwork cache; the only double
is the network (a recording HttpGet answering with Deezer's real shapes).

[if] a store cover is listed, previewed, saved or skipped over HTTP or the CLI [then] only a consented cover is ever cached, [else stop].

Regression one-liners:
  - if a pending suggestion is not listed with its consent note then broken
  - if the preview image is cached to disk then broken
  - if Save all does not cache every pending cover then broken
  - if Skip does not stop the track being suggested again then broken
  - if decide with nothing named is accepted then broken
  - if an image off the store's own host is fetched then broken
"""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from apps.adapters.rekordbox import paths
from apps.opendj_cli import artwork_cli
from apps.shared import artwork_sources as art
from apps.shared.artwork_sources import ArtworkCache, StoreSuggestion
from apps.webui.server.app import create_app
from apps.webui.server.routes import artwork_suggestions

pytestmark = pytest.mark.requirement("LIBM-189")

COVER_URL = "https://e-cdns-images.dzcdn.net/images/cover/abc/500x500-000000-80-0-0.jpg"


def _jpeg() -> bytes:
    out = BytesIO()
    Image.new("RGB", (16, 16), (10, 120, 200)).save(out, format="JPEG")
    return out.getvalue()


class RecordingHttp:
    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        return self.routes.get(url, (404, b""))


def suggestion(sid: str, url: str = COVER_URL) -> StoreSuggestion:
    return StoreSuggestion("deezer", url, f"Title {sid}", "ASCO", f"Title {sid}", "ASCO")


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = ArtworkCache(tmp_path / "artwork-cache")
    monkeypatch.setattr(paths, "online_artwork_cache", lambda: cache)
    http = RecordingHttp({COVER_URL: (200, _jpeg())})
    monkeypatch.setattr(art, "_HTTP", http)
    client = TestClient(create_app(mount_frontend=False, enable_cors=False))
    return client, cache, http


def test_pending_list_carries_the_consent_note(rig) -> None:
    client, cache, _http = rig
    cache.put_suggestion("a", suggestion("a"))
    doc = client.get("/api/v1/artwork/suggestions").json()
    assert doc["note"] == artwork_suggestions.CONSENT_NOTE
    assert doc["note"].startswith("* Deezer and iTunes terms do not allow caching")
    assert [(p["stable_id"], p["source_label"]) for p in doc["pending"]] == [("a", "Deezer")]
    assert doc["pending"][0]["image_url"] == "/api/v1/artwork/suggestions/a/image"


def test_preview_image_is_fetched_live_and_never_stored(rig, tmp_path: Path) -> None:
    client, cache, http = rig
    cache.put_suggestion("a", suggestion("a"))
    image = client.get("/api/v1/artwork/suggestions/a/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert image.headers["cache-control"] == "no-store"
    assert http.calls == [COVER_URL]
    assert not list((tmp_path / "artwork-cache").glob("*.img"))
    assert cache.suggestion("a") is not None, "a preview is not a decision"
    assert client.get("/api/v1/artwork/suggestions/zz/image").status_code == 404


def test_off_host_image_is_refused(rig) -> None:
    client, cache, http = rig
    cache.put_suggestion("a", suggestion("a", "https://evil.example/x.jpg"))
    assert client.get("/api/v1/artwork/suggestions/a/image").status_code == 502
    assert http.calls == []


def test_save_all_caches_every_pending_cover(rig) -> None:
    client, cache, _http = rig
    cache.put_suggestion("a", suggestion("a"))
    cache.put_suggestion("b", suggestion("b"))
    out = client.post("/api/v1/artwork/suggestions/decide", json={"accept_all": True}).json()
    assert {sid: r["decision"] for sid, r in out["results"].items()} == {"a": "accepted", "b": "accepted"}
    assert out["pending"] == []
    assert cache.has("a") and cache.has("b")


def test_skip_is_remembered_and_selected_only(rig) -> None:
    client, cache, _http = rig
    cache.put_suggestion("a", suggestion("a"))
    cache.put_suggestion("b", suggestion("b"))
    out = client.post("/api/v1/artwork/suggestions/decide", json={"skip": ["a"]}).json()
    assert out["results"] == {"a": {"decision": "skipped", "source": "deezer"}}
    assert [p["stable_id"] for p in out["pending"]] == ["b"], "an unticked cover stays pending"
    assert cache.store_declined("a") and not cache.has("a")


def test_decide_needs_a_target(rig) -> None:
    client, _cache, _http = rig
    assert client.post("/api/v1/artwork/suggestions/decide", json={}).status_code == 422


def test_enrich_summary_counts_the_pending_covers(rig) -> None:
    client, cache, _http = rig
    cache.put_suggestion("a", suggestion("a"))
    summary = client.get("/api/v1/enrich/summary").json()
    assert summary["artwork_suggestions"] == 1 and summary["show"] is True


def test_cli_request_shapes() -> None:
    assert artwork_cli.request_for("list", [], False) == ("GET", "/api/v1/artwork/suggestions", None)
    assert artwork_cli.request_for("accept", [], True)[2] == {"accept_all": True}
    assert artwork_cli.request_for("skip", ["a", "b"], False)[2] == {"skip": ["a", "b"]}
    for bad in (("accept", [], False), ("skip", ["a"], True), ("list", ["a"], False)):
        with pytest.raises(ValueError):
            artwork_cli.request_for(*bad)


@pytest.mark.requirement("LIBM-188")
def test_ingest_outcome_reports_a_suggestion_not_a_miss(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a track has a pending store suggestion [then] backfill reports suggested not miss, [else stop]."""
    from apps.webui.server import artwork_backfill

    _client, cache, _http = rig
    cache.put_suggestion("a", suggestion("a"))
    monkeypatch.setattr(paths, "local_artwork_available", lambda _sid: False)
    monkeypatch.setattr(paths, "online_track_query", lambda _sid: art.TrackQuery("ASCO", "Title a", None))
    outcome = artwork_backfill.cover_outcome("a")
    assert outcome.status == "suggested" and "deezer" in outcome.reason
    assert json.dumps(outcome.reason)  # serialisable for the status document
